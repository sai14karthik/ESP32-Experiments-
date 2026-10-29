# Copyright 2025, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import io
import json

import numpy as np
#from rpm.boxmot.boxmot.trackers.strongsort.sort import tracker
#import torchvision.transforms as transforms
import torch
import time

from torch.utils.dlpack import from_dlpack, to_dlpack

# triton_python_backend_utils is available in every Triton Python model. You
# need to use this module to create inference requests and responses. It also
# contains some utility functions for extracting information from model_config
# and converting Triton input/output types to numpy types.
import triton_python_backend_utils as pb_utils
import cv2
import torch
from torchvision.transforms import functional as F
## Copied from mmpose0.29 mmpose/core/bbox/transforms.py

def old_bbox_xyxy2xywh(bbox_xyxy):
    """Transform the bbox format from x1y1x2y2 to xywh.

    Args:
        bbox_xyxy (np.ndarray): Bounding boxes (with scores), shaped (n, 4) or
            (n, 5). (left, top, right, bottom, [score])

    Returns:
        np.ndarray: Bounding boxes (with scores),
          shaped (n, 4) or (n, 5). (left, top, width, height, [score])
    """
    bbox_xywh = bbox_xyxy.copy()
    #bbox_xywh[2] = bbox_xywh[2] - bbox_xywh[0]
    #bbox_xywh[3] = bbox_xywh[3] - bbox_xywh[1]

    return bbox_xywh

def old_bbox_xywh2cs(bbox, aspect_ratio, padding=1., pixel_std=200.):
    """Transform the bbox format from (x,y,w,h) into (center, scale)

    Args:
        bbox (ndarray): Single bbox in (x, y, w, h)
        aspect_ratio (float): The expected bbox aspect ratio (w over h)
        padding (float): Bbox padding factor that will be multilied to scale.
            Default: 1.0
        pixel_std (float): The scale normalization factor. Default: 200.0

    Returns:
        tuple: A tuple containing center and scale.
        - np.ndarray[float32](2,): Center of the bbox (x, y).
        - np.ndarray[float32](2,): Scale of the bbox w & h.
    """

    x, y, w, h = bbox[:4]
    center = np.array([x + w * 0.5, y + h * 0.5], dtype=np.float32)

    if w > aspect_ratio * h:
        h = w * 1.0 / aspect_ratio
    elif w < aspect_ratio * h:
        w = h * aspect_ratio

    scale = np.array([w, h], dtype=np.float32) / pixel_std
    scale = scale * padding

    return center, scale

def _get_3rd_point(a, b):
    """To calculate the affine matrix, three pairs of points are required. This
    function is used to get the 3rd point, given 2D points a & b.

    The 3rd point is defined by rotating vector `a - b` by 90 degrees
    anticlockwise, using b as the rotation center.

    Args:
        a (np.ndarray): point(x,y)
        b (np.ndarray): point(x,y)

    Returns:
        np.ndarray: The 3rd point.
    """
    assert len(a) == 2
    assert len(b) == 2
    direction = a - b
    third_pt = b + np.array([-direction[1], direction[0]], dtype=np.float32)

    return third_pt

def rotate_point(pt, angle_rad):
    """Rotate a point by an angle.

    Args:
        pt (list[float]): 2 dimensional point to be rotated
        angle_rad (float): rotation angle by radian

    Returns:
        list[float]: Rotated point.
    """
    assert len(pt) == 2
    sn, cs = np.sin(angle_rad), np.cos(angle_rad)
    new_x = pt[0] * cs - pt[1] * sn
    new_y = pt[0] * sn + pt[1] * cs
    rotated_pt = [new_x, new_y]

    return rotated_pt



def get_affine_transform(center,
                         scale,
                         rot,
                         output_size,
                         shift=(0., 0.),
                         inv=False):
    """Get the affine transform matrix, given the center/scale/rot/output_size.

    Args:
        center (np.ndarray[2, ]): Center of the bounding box (x, y).
        scale (np.ndarray[2, ]): Scale of the bounding box
            wrt [width, height].
        rot (float): Rotation angle (degree).
        output_size (np.ndarray[2, ] | list(2,)): Size of the
            destination heatmaps.
        shift (0-100%): Shift translation ratio wrt the width/height.
            Default (0., 0.).
        inv (bool): Option to inverse the affine transform direction.
            (inv=False: src->dst or inv=True: dst->src)

    Returns:
        np.ndarray: The transform matrix.
    """
    assert len(center) == 2
    assert len(scale) == 2
    assert len(output_size) == 2
    assert len(shift) == 2

    # pixel_std is 200.
    scale_tmp = scale * 200.0

    shift = np.array(shift)
    src_w = scale_tmp[0]
    dst_w = output_size[0]
    dst_h = output_size[1]

    rot_rad = np.pi * rot / 180
    src_dir = rotate_point([0., src_w * -0.5], rot_rad)
    dst_dir = np.array([0., dst_w * -0.5])

    src = np.zeros((3, 2), dtype=np.float32)
    src[0, :] = center + scale_tmp * shift
    src[1, :] = center + src_dir + scale_tmp * shift
    src[2, :] = _get_3rd_point(src[0, :], src[1, :])

    dst = np.zeros((3, 2), dtype=np.float32)
    dst[0, :] = [dst_w * 0.5, dst_h * 0.5]
    dst[1, :] = np.array([dst_w * 0.5, dst_h * 0.5]) + dst_dir
    dst[2, :] = _get_3rd_point(dst[0, :], dst[1, :])

    if inv:
        trans = cv2.getAffineTransform(np.float32(dst), np.float32(src))
    else:
        trans = cv2.getAffineTransform(np.float32(src), np.float32(dst))

    return trans


def hpe_preprocessing(image,
                      bbox,
                      tracked_id,
                      image_size,
                      padding,
                      pixel_std,
                      mean,
                      std,
                      device="cuda"):
    """Get the crop of the image for the bbox with padding and resize"""

    #print("bbox.shape", bbox.shape)
    #print("tracked_id.shape", tracked_id.shape)
    #bbox = bbox[1:5]
    #image = image
    #print("---bbox.shape", bbox.shape)
    #tracked_id = tracked_id[1:]
    # Find center and scale
    aspect_ratio = image_size[0] / image_size[1]
    #print(image.shape)

    bbox_xywh = old_bbox_xyxy2xywh(bbox)
    #print ("bbox_xywh->", bbox_xywh)

    c, s = old_bbox_xywh2cs(
        bbox_xywh,
        aspect_ratio=aspect_ratio,
        padding=padding,
        pixel_std=pixel_std)
    r = 0

    # Extract and warp bbox with padding to create 256x256 image
    trans = get_affine_transform(c, s, r, image_size)
    #print ("trans.shape", trans.shape)
    #print ("image.shape", image.shape)
    img = cv2.warpAffine(
            image,
            trans, (int(image_size[0]), int(image_size[1])),
            flags=cv2.INTER_LINEAR)
    #print ("img.shape", img.shape)

    # Convert to tensor to normalize

    img_tensor = torch.from_numpy(img.astype('float32')).permute(
        2, 0, 1).unsqueeze(0).to(device).div_(255.0)
    # Normalize
    img_tensor = F.normalize(img_tensor, mean=mean, std=std, inplace=True)
    #print ("img_tensor.shape", img_tensor.shape)

    return img_tensor.contiguous(), c, s


def convert_pct2rpm(pose):

# PCT/COCO Joints:  0:nose, 1:left_eye, 2:right_eye, 3:left_ear, 4:right_ear, 5:left_shoulder, 6:right_shoulder, 7:left_elbow, 8:right_elbow,
#                   9:left_wrist, 10:right_wrist,  11:left_hip, 12:right_hip, 13:left_knee, 14:right_knee, 15:left_ankle, 16: right_ankle


# RPM Joints: 1: base of spine, 2: middle of spine, 3: neck, 4: left shoulder, 5: left elbow, 6: left wrist, 7: right shoulder, 8: right elbow, 9: right wrist,
#             10: left hip, 11: left knee, 12: left ankle, 13: right hip, 14: right knee, 15: right ankle, 16: nose, 17: left ear, 18: right ear


    rpm_pose = np.zeros((1,18,3), dtype=np.float32)

    # 1: base of spine (Derived from left hip and right hip)
    rpm_pose[0,0] = (pose[11] + pose[12])*.5
    # 3: neck (derived from left shoulder and right shoulder)
    rpm_pose[0,2] = (pose[5] + pose[6])*.5
    # 2: middle of spine (derived from base of spine and neck)
    rpm_pose[0,1] = (rpm_pose[0,0] + rpm_pose[0,2])*.5
    # 4: left shoulder
    rpm_pose[0,3] = pose[5]
    # 5: left elbow
    rpm_pose[0,4] = pose[7]
    # 6: left wrist
    rpm_pose[0,5] = pose[9]
    # 7: right shoulder
    rpm_pose[0,6] = pose[6]
    # 8: right elbow
    rpm_pose[0,7] = pose[8]
    # 9: right wrist
    rpm_pose[0,8] = pose[10]
    # 10: left hip
    rpm_pose[0,9] = pose[11]
    # 11: left knee
    rpm_pose[0,10] = pose[13]
    # 12: left ankle
    rpm_pose[0,11] = pose[15]
    # 13: right hip
    rpm_pose[0,12] = pose[12]
    # 14: right knee
    rpm_pose[0,13] = pose[14]
    # 15: left ankle
    rpm_pose[0,14] = pose[16]
    # 16: nose
    rpm_pose[0,15] = pose[0]
    # 17: left ear:
    rpm_pose[0,16] = pose[3]
    # 18: right ear
    rpm_pose[0,17] = pose[4]

    return(rpm_pose)

def transform_preds(coords, center, scale, output_size, use_udp=False):
    """Get final keypoint predictions from heatmaps and apply scaling and
    translation to map them back to the image.

    Note:
        num_keypoints: K

    Args:
        coords (np.ndarray[K, ndims]):

            * If ndims=2, corrds are predicted keypoint location.
            * If ndims=4, corrds are composed of (x, y, scores, tags)
            * If ndims=5, corrds are composed of (x, y, scores, tags,
              flipped_tags)

        center (np.ndarray[2, ]): Center of the bounding box (x, y).
        scale (np.ndarray[2, ]): Scale of the bounding box
            wrt [width, height].
        output_size (np.ndarray[2, ] | list(2,)): Size of the
            destination heatmaps.
        use_udp (bool): Use unbiased data processing

    Returns:
        np.ndarray: Predicted coordinates in the images.
    """
    assert coords.shape[1] in (2, 4, 5)
    assert len(center) == 2
    assert len(scale) == 2
    assert len(output_size) == 2

    # Recover the scale which is normalized by a factor of 200.
    scale = scale * 200.0


    if use_udp:
        scale_x = scale[0] / (output_size[0] - 1.0)
        scale_y = scale[1] / (output_size[1] - 1.0)
    else:
        scale_x = scale[0] / output_size[0]
        scale_y = scale[1] / output_size[1]

    target_coords = np.ones_like(coords)
    target_coords[:, 0] = coords[:, 0] * scale_x + center[0] - scale[0] * 0.5
    target_coords[:, 1] = coords[:, 1] * scale_y + center[1] - scale[1] * 0.5

    return target_coords

def hpe_postprocess(center, scale, preds, scores, img_shape):
    """Performs post-processing of the joints to translate to original image
    """

    c = center
    s = scale

    p_joints = preds
    score_pose = scores
    #print(p_joints)
    p_joints = transform_preds(p_joints, c, s, img_shape, use_udp=False)


    all_preds = np.zeros((1, p_joints.shape[0], 3), dtype=np.float32)
    all_preds[:, :, 0:2] = p_joints
    all_preds[:, :, 2:3] = score_pose
    #print (all_preds)

    return(all_preds)

class TritonPythonModel:
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    def initialize(self, args):
        """`initialize` is called only once when the model is being loaded.
        Implementing `initialize` function is optional. This function allows
        the model to initialize any state associated with this model.

        Parameters
        ----------
        args : dict
          Both keys and values are strings. The dictionary keys and values are:
          * model_config: A JSON string containing the model configuration
          * model_instance_kind: A string containing model instance kind
          * model_instance_device_id: A string containing model instance device ID
          * model_repository: Model repository path
          * model_version: Model version
          * model_name: Model name
        """

        # You must parse model_config. JSON string is not parsed here
        self.model_config = model_config = json.loads(args["model_config"])

        # Pixel std is 200.0, which serves as the normalization factor to
        # to calculate bbox scales.
        self.pixel_std = 200.0
        self.image_size = np.array([256, 256])
        self.padding = 1.12
        self.mean=[0.485, 0.456, 0.406]
        self.std=[0.229, 0.224, 0.225]
        self.action_history = {}
        self.sendstartframe_toar = 0
        self.device = f"cuda:{args.get('model_instance_device_id', '0')}"

        # Get OUTPUT1 configuration
        output1_config = pb_utils.get_output_config_by_name(model_config, "poses")
        output2_config = pb_utils.get_output_config_by_name(model_config, "bbox")
        output3_config = pb_utils.get_output_config_by_name(model_config, "tracked_id")
        output4_config = pb_utils.get_output_config_by_name(model_config, "action_predicted_flag")
        output5_config = pb_utils.get_output_config_by_name(model_config, "predicted_index")
        output6_config = pb_utils.get_output_config_by_name(model_config, "ar_confidence")

        # Convert Triton types to numpy types
        self.output1_dtype = pb_utils.triton_string_to_numpy(
            output1_config["data_type"]
        )
        self.output2_dtype = pb_utils.triton_string_to_numpy(
            output2_config["data_type"]
        )
        self.output3_dtype = pb_utils.triton_string_to_numpy(
            output3_config["data_type"]
        )
        self.output4_dtype = pb_utils.triton_string_to_numpy(
            output4_config["data_type"]
        )
        self.output5_dtype = pb_utils.triton_string_to_numpy(
            output5_config["data_type"]
        )
        self.output6_dtype = pb_utils.triton_string_to_numpy(
            output6_config["data_type"]
        )
    def np_from_pb_tensor(self, pb_tensor):
        dlpack_tensor = pb_tensor.to_dlpack()

        # Convert DLPack tensor to PyTorch tensor
        torch_tensor = from_dlpack(dlpack_tensor)
        # Move the tensor to CPU memory
        cpu_tensor = torch_tensor.cpu()
        np_array = cpu_tensor.detach().numpy()
        return np_array

    def execute(self, requests):
        """`execute` MUST be implemented in every Python model. `execute`
        function receives a list of pb_utils.InferenceRequest as the only
        argument. This function is called when an inference request is made
        for this model. Depending on the batching configuration (e.g. Dynamic
        Batching) used, `requests` may contain multiple requests. Every
        Python model, must create one pb_utils.InferenceResponse for every
        pb_utils.InferenceRequest in `requests`. If there is an error, you can
        set the error argument when creating a pb_utils.InferenceResponse

        Parameters
        ----------
        requests : list
          A list of pb_utils.InferenceRequest

        Returns
        -------
        list
          A list of pb_utils.InferenceResponse. The length of this list must
          be the same as `requests`
        """

        output1_dtype = self.output1_dtype
        output2_dtype = self.output2_dtype
        output3_dtype = self.output3_dtype
        output4_dtype = self.output4_dtype
        output5_dtype = self.output5_dtype
        output6_dtype = self.output6_dtype

        responses = []

        start_time = time.time()
        # Every Python backend must iterate over everyone of the requests
        # and create a pb_utils.InferenceResponse for each of them.
        #print (f"Total Requests: {len(requests)}")
        for request in requests:
            # Get INPUT0
            in_1 = pb_utils.get_input_tensor_by_name(request, "image")

            # Convert pb_utils.Tensor to DLPack tensor
            dlpack_tensor = in_1.to_dlpack()
            # Convert DLPack tensor to PyTorch tensor
            torch_tensor = from_dlpack(dlpack_tensor)
            # Move the tensor to CPU memory
            cpu_tensor = torch_tensor.cpu()
            image = cpu_tensor.detach().numpy()

            # Get INPUT1
            in_2 = pb_utils.get_input_tensor_by_name(request, "bbox")

            # Convert pb_utils.Tensor to DLPack tensor
            dlpack_tensor = in_2.to_dlpack()
            # Convert DLPack tensor to PyTorch tensor
            torch_tensor = from_dlpack(dlpack_tensor)
            # Move the tensor to CPU memory
            cpu_tensor = torch_tensor.cpu()
            bbox = cpu_tensor.detach().numpy()


            # Get INPUT1
            in_3 = pb_utils.get_input_tensor_by_name(request, "tracked_id")

            # Convert pb_utils.Tensor to DLPack tensor
            dlpack_tensor = in_3.to_dlpack()
            # Convert DLPack tensor to PyTorch tensor
            torch_tensor = from_dlpack(dlpack_tensor)
            # Move the tensor to CPU memory
            cpu_tensor = torch_tensor.cpu()
            tracked_id = cpu_tensor.detach().numpy()

            in_4 = pb_utils.get_input_tensor_by_name(request, "start_frame")

            # Convert pb_utils.Tensor to DLPack tensor
            dlpack_tensor = in_4.to_dlpack()
            # Convert DLPack tensor to PyTorch tensor
            torch_tensor = from_dlpack(dlpack_tensor)
            # Move the tensor to CPU memory
            cpu_tensor = torch_tensor.cpu()
            start_frame = cpu_tensor.detach().numpy()

            #print(f"start_frame[0]: {start_frame[0]}")
            # clear the local history when it is the first frame
            # Also send start of the frame flag to bls_ar model only on
            # the first call to bls_ar after the start_frame[0][0] == 1
            # is received
            if start_frame[0][0] == 1:
                self.action_history.clear()
                self.sendstartframe_toar = 1
                print(f"########cleared action_history")


            #print("dlpack--------- %s seconds ----------------" % (time.time() - start_time))

            #print (tracked_id_length)
            all_rpm_pose = []
            for i in range(tracked_id.shape[0]):

                tracked_id_length = tracked_id[i][0]
                if tracked_id_length == 0:
                    rpm_pose = np.zeros((1, 18, 3), dtype=np.float32)
                    all_rpm_pose.append(rpm_pose)
                    action_predicted_flag = np.zeros((1, 1), dtype=np.int32)
                    predicted_index = np.zeros((1, 1), dtype=np.int32)
                    ar_confidence = np.zeros((1, 1), dtype=np.float32)
                else:
                    # Create HPE PCT model inputs
                    for j in range(tracked_id_length):
                        img_tensor, center, scale = hpe_preprocessing(
                            image[i], bbox[i][1+j*4:5+j*4], tracked_id[i][j],
                            self.image_size, self.padding, self.pixel_std,
                            self.mean, self.std, self.device)

                        # Main HPE ONNX model trtengine call with input and output packaging
                        hpe_inputs = []
                        #hpe_dlpack_tensor = torch.utils.dlpack.to_dlpack(img_tensor)
                        #hpt_PbTensor = pb_utils.Tensor
                        hpe_inputs.append(pb_utils.Tensor.from_dlpack(
                            "input", to_dlpack(img_tensor)))

                        # Create inference request object
                        infer_request = pb_utils.InferenceRequest(
                            model_name="hpe",
                            requested_output_names=["scores", "preds"],
                            inputs=hpe_inputs,
                        )

                        # Perform synchronous blocking inference request
                        infer_response = infer_request.exec()
                        # Make sure that the inference response doesn't have an error. If
                        # it has an error and you can't proceed with your model execution
                        # you can raise an exception.
                        if infer_response.has_error():
                            raise pb_utils.TritonModelException(infer_response.error().message())

                        for output_tensor in infer_response.output_tensors():
                            if output_tensor.name() == "scores":
                                scores_pbTensor = output_tensor
                            if output_tensor.name() == "preds":
                                preds_pbTensor = output_tensor


                        #[preds_pbTensor, scores_pbTensor] = infer_response.output_tensors()
                        # Convert pb_utils.Tensor to DLPack tensor
                        dlpack_tensor = scores_pbTensor.to_dlpack()
                        # Convert DLPack tensor to PyTorch tensor
                        torch_tensor = from_dlpack(dlpack_tensor)
                        # Move the tensor to CPU memory
                        cpu_tensor = torch_tensor.cpu()
                        scores = cpu_tensor.detach().numpy()

                        # Convert pb_utils.Tensor to DLPack tensor
                        dlpack_tensor = preds_pbTensor.to_dlpack()
                        # Convert DLPack tensor to PyTorch tensor
                        torch_tensor = from_dlpack(dlpack_tensor)
                        # Move the tensor to CPU memory
                        cpu_tensor = torch_tensor.cpu()
                        preds = cpu_tensor.detach().numpy()

                        pose = hpe_postprocess(center, scale, preds[0,:,:], scores[0,:,:], self.image_size)
                        rpm_pose = convert_pct2rpm(pose[0])
                        ar_inputs = []

                        #hpe_dlpack_tensor = torch.utils.dlpack.to_dlpack(img_tensor)
                        #hpt_PbTensor = pb_utils.Tensor
                        #img, center, scale = hpe_preprocessing(image[i], bbox[i][1+j*4:5+j*4], tracked_id[i][j],
                        #print(f"rpm_pose:{rpm_pose.shape}, bbox:{bbox.shape}, tracked_id:{tracked_id.shape}")

                        # Send start of the frame flag to bls_ar model only when
                        # self.sendstartframe_toar == 1 and reset sendstartframe_toar
                        arstrtfrm = np.zeros((1), dtype=np.uint8)
                        if self.sendstartframe_toar == 1:
                            arstrtfrm[0] = 1
                            self.sendstartframe_toar = 0

                        ar_inputs.append(pb_utils.Tensor("poses",rpm_pose[0]))
                        ar_inputs.append(pb_utils.Tensor("bbox", bbox[0][0:5])) #bbox[i][1+j*4:5+j*4]))
                        ar_inputs.append(pb_utils.Tensor("tracked_id", tracked_id[0][0:2])) #tracked_id[i]))
                        ar_inputs.append(pb_utils.Tensor("start_frame", arstrtfrm))

                        #print(rpm_pose, bbox, tracked_id)
                        # Create inference request object
                        infer_request_ar = pb_utils.InferenceRequest(
                            model_name="bls_ar",
                            requested_output_names=["action_predicted_flag", "predicted_index", "ar_confidence"],
                            inputs=ar_inputs,
                        )

                        # Perform synchronous blocking inference request
                        infer_response_ar = infer_request_ar.exec()
                        # Make sure that the inference response doesn't have an error. If
                        # it has an error and you can't proceed with your model execution
                        # you can raise an exception.
                        if infer_response_ar.has_error():
                            raise pb_utils.TritonModelException(infer_response_ar.error().message())


                        for output_tensor in infer_response_ar.output_tensors():
                            if output_tensor.name() == "action_predicted_flag":
                                action_predicted_flag_pbTensor = output_tensor
                                action_predicted_flag = self.np_from_pb_tensor(action_predicted_flag_pbTensor)
                            if output_tensor.name() == "predicted_index":
                                predicted_index_pbTensor = output_tensor
                                predicted_index = self.np_from_pb_tensor(predicted_index_pbTensor)
                            if output_tensor.name() == "ar_confidence":
                                ar_confidence_pbTensor = output_tensor
                                ar_confidence = self.np_from_pb_tensor(ar_confidence_pbTensor)

                        # print(f"action_predicted_flag:{action_predicted_flag}, predicted_index:{predicted_index}, ar_confidence:{ar_confidence}")
                        if action_predicted_flag[0] == 1:
                            self.action_history[tracked_id[0][1]] = (predicted_index[0], ar_confidence[0])
                        else:
                            if tracked_id[0][1] in self.action_history:
                                predicted_index[0] = self.action_history[tracked_id[0][1]][0]
                                ar_confidence[0] = self.action_history[tracked_id[0][1]][1]
                                action_predicted_flag[0] = 1
                            else:
                                predicted_index[0] = -1
                                ar_confidence[0] = 0


                        #print (f"rpm_pose.shape: {rpm_pose.shape}")
                    all_rpm_pose.append(rpm_pose)
                    #print(rpm_pose)
            rpm_pose_batch = np.concatenate(all_rpm_pose, axis=0)
            out_tensor_1 = pb_utils.Tensor("poses", rpm_pose_batch.astype(output1_dtype))
            out_tensor_2 = pb_utils.Tensor("bbox", bbox.astype(output2_dtype))
            out_tensor_3 = pb_utils.Tensor("tracked_id", tracked_id.astype(output3_dtype))
            out_tensor_4 = pb_utils.Tensor("action_predicted_flag", action_predicted_flag.astype(output4_dtype))
            out_tensor_5 = pb_utils.Tensor("predicted_index", predicted_index.astype(output5_dtype))
            out_tensor_6 = pb_utils.Tensor("ar_confidence", ar_confidence.astype(output6_dtype))

           # print(out_tensor_1)


            # Create InferenceResponse. You can set an error here in case
            # there was a problem with handling this inference request.
            # Below is an example of how you can set errors in inference
            # response:
            #
            # pb_utils.InferenceResponse(
            #    output_tensors=..., TritonError("An error occurred"))
            inference_response = pb_utils.InferenceResponse(
                output_tensors=[out_tensor_1, out_tensor_2, out_tensor_3, out_tensor_4, out_tensor_5, out_tensor_6]
            )
            responses.append(inference_response)

            stop_time = time.time()
            #print("------------------------------------- %s seconds --------------------------------------" % (stop_time - start_time))

        # You should return a list of pb_utils.InferenceResponse. Length
        # of this list must match the length of `requests` list.
        return responses

    def finalize(self):
        """`finalize` is called only once when the model is being unloaded.
        Implementing `finalize` function is OPTIONAL. This function allows
        the model to perform any necessary clean ups before exit.
        """
        print("Cleaning up...")
