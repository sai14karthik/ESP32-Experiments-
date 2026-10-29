# Copyright 2021-2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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
import torchvision.transforms as transforms
import torch
import time
import copy

from torch.utils.dlpack import from_dlpack, to_dlpack

# triton_python_backend_utils is available in every Triton Python model. You
# need to use this module to create inference requests and responses. It also
# contains some utility functions for extracting information from model_config
# and converting Triton input/output types to numpy types.
import triton_python_backend_utils as pb_utils
import torch
import random
import math

## Copied from mmpose0.29 mmpose/core/bbox/transforms.py
def rand_view_transform(X, agx, agy, s):
    agx = math.radians(agx)
    agy = math.radians(agy)
    Rx = np.asarray([[1, 0, 0], [0, math.cos(agx), math.sin(agx)], [0, -math.sin(agx), math.cos(agx)]])
    Ry = np.asarray([[math.cos(agy), 0, -math.sin(agy)], [0, 1, 0], [math.sin(agy), 0, math.cos(agy)]])
    Ss = np.asarray([[s, 0, 0], [0, s, 0], [0, 0, s]])
    X0 = np.dot(np.reshape(X, (-1, 3)), np.dot(Ry, np.dot(Rx, Ss)))
    X = np.reshape(X0, X.shape)
    return X
def ar_get_data(value, partition, new_idx):

    random.random()
    agx = 0
    agy = 0
    s = 1.0
    time_steps = 64

    #print("value.shape", value.shape)

    center = value[0, 1, :]
    value = value - center
    scalerValue = rand_view_transform(value, agx, agy, s)

    scalerValue = np.reshape(scalerValue, (-1, 3))
    scalerValue = (scalerValue - np.min(scalerValue, axis=0)) / (
                np.max(scalerValue, axis=0) - np.min(scalerValue, axis=0))
    scalerValue = scalerValue * 2 - 1

    scalerValue = np.reshape(scalerValue, (-1, 18, 3))

    data = np.zeros((time_steps, 18, 3))

    value = scalerValue[:, :, :]
    length = value.shape[0]

    idx = np.linspace(0, length - 1, time_steps).astype(np.int32)
    data[:, :, :] = value[idx, :, :]
    index_t = 2 * idx.astype(np.float32) / length - 1

    data = np.transpose(data, (2, 0, 1))
    C, T, V = data.shape
    data = np.reshape(data, (C, T, V, 1))

    if partition:
        data = data[:, :, new_idx]

    return data, index_t

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

        # initialize tracked_id to -1 and poses to zeros
        self.action_frame_buffer_len = 50            # Trained with 50 skeletons
        self.action_decision_periodicity_len = 10 # Decide action every 1 second
        self.action_frame_first_decision_len = 30  # Very first decision
        self.action_frame_hangover_len = 5 # Hangover frames
        self.action_active_ids = []
        self.action_active_counts = []
        self.action_inactive_ids = []
        self.action_inactive_counts = []
        self.action_active_skeletons = []
        self.width = 960
        self.height = 540

        ## Prepare skeleton partition partition 18 joints into 6 groups of 3
        partition = True  ## Always true
        if partition:
            right_arm = np.array([7, 8, 9]) - 1
            left_arm = np.array([4, 5, 6]) - 1
            right_leg = np.array([13, 14, 15]) - 1
            left_leg = np.array([10, 11, 12]) - 1
            torso = np.array([2, 3, 1]) - 1
            head = np.array([16, 17, 18]) -1
            new_idx = np.concatenate((right_arm, left_arm, right_leg, left_leg, head, torso), axis=-1)

        self.ar_partition = partition
        self.ar_new_idx = new_idx



        # Get OUTPUT1 configuration
        output1_config = pb_utils.get_output_config_by_name(model_config, "predicted_index")
        output2_config = pb_utils.get_output_config_by_name(model_config, "ar_confidence")
        output3_config = pb_utils.get_output_config_by_name(model_config, "action_predicted_flag")

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

    def normalize_skeletons(self, pose):
        normalized_pose = copy.deepcopy(pose)

        normalized_pose[:,0] = 2.0*(normalized_pose[:,0])/(self.width) - 1.0
        normalized_pose[:,1] = -2.0*(normalized_pose[:,1])/(self.height) + 1.0
        # Return normalized pose as a list for JSON output

        return(normalized_pose.tolist())

    def prep_ar_input(self, poses, bbox, tracked_id):

        skeleton_timeseries_ready_flag = False
        ids_ready = []
        skeletons_ready = []


        # print(f"poses, {poses.shape}, bbox, {bbox.shape}, tracked_id, {tracked_id} {tracked_id.shape}")
        #if tracked_id[0][0] != 0:
        #    patient_ids = list(tracked_id[0][1:tracked_id[0][0]+1].astype(int))
        if bbox.shape[0] != 1:
            patient_ids = list(tracked_id[1:].astype(int))
        else:
            patient_ids = []

        # print("patient_ids", patient_ids)
        ## Assumes single patient

        for i, patient_id in enumerate(patient_ids):
            if patient_id not in self.action_active_ids:
                # print("patient_id not in self.action_active_ids", patient_id)
                self.action_active_ids.append(patient_id)
                self.action_active_counts.append(1)
                self.action_active_skeletons.append([self.normalize_skeletons(poses)])
            else:
                index = self.action_active_ids.index(patient_id)
                self.action_active_counts[index] = self.action_active_counts[index]+1
                self.action_active_skeletons[index].append(self.normalize_skeletons(poses))
                if self.action_active_counts[index] == self.action_frame_buffer_len:
                    self.action_active_counts[index] = self.action_active_counts[index] - self.action_decision_periodicity_len

                    # print("patient_id in self.action_active_ids", patient_id, "skeleton_timeseries_ready_flag", skeleton_timeseries_ready_flag)
                    skeleton_timeseries_ready_flag = True
                    ids_ready.append(self.action_active_ids[index])
                    skeletons_ready.append(copy.deepcopy(self.action_active_skeletons[index]))

                    # Remove first periodicity_len frames from buffer
                    for i in range(self.action_decision_periodicity_len):
                        del self.action_active_skeletons[index][0]


        ## Purging inactive ids
        for active_id in self.action_active_ids:
            if active_id not in patient_ids:
                if active_id not in self.action_inactive_ids:
                    self.action_inactive_ids.append(active_id)
                    self.action_inactive_counts.append(1)
                else:
                    index = self.action_inactive_ids.index(active_id)
                    self.action_inactive_counts[index] = self.action_inactive_counts[index]+1
                    if self.action_inactive_counts[index] == self.action_frame_hangover_len:
                        #print("Remove this id {}".format(index))
                        del self.action_inactive_ids[index]
                        del self.action_inactive_counts[index]
                        index = self.action_active_ids.index(active_id)
                        del self.action_active_ids[index]
                        del self.action_active_counts[index]
                        del self.action_active_skeletons[index]
        #print(frame_index, self.action_active_counts, self.action_active_ids)

        return(skeleton_timeseries_ready_flag, ids_ready, skeletons_ready)


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

        responses = []

        start_time = time.time()
        # Every Python backend must iterate over everyone of the requests
        # and create a pb_utils.InferenceResponse for each of them.
        #print (f"Total Requests: {len(requests)}")
        for request in requests:
            # Get INPUT0
            in_1 = pb_utils.get_input_tensor_by_name(request, "poses")

            # Convert pb_utils.Tensor to DLPack tensor
            dlpack_tensor = in_1.to_dlpack()
            # Convert DLPack tensor to PyTorch tensor
            torch_tensor = from_dlpack(dlpack_tensor)
            # Move the tensor to CPU memory
            cpu_tensor = torch_tensor.cpu()
            poses = cpu_tensor.detach().numpy()

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

            #print(f"#### In bls_ar model.py start_frame[0]: {start_frame[0]}")

            # Clear the state when it is the first frame
            if start_frame[0] == 1:
                self.action_active_ids = []
                self.action_active_counts = []
                self.action_active_skeletons = []
                self.action_inactive_ids = []
                self.action_inactive_counts = []
                print(f"#### In bls_ar model.py cleared the state")


            #print("dlpack--------- %s seconds ----------------" % (time.time() - start_time))

            skeleton_timeseries_ready_flag, ids_ready, skeletons_ready = self.prep_ar_input(poses, bbox, tracked_id)

            if skeleton_timeseries_ready_flag:
                #print("skeleton_timeseries_ready_flag", skeleton_timeseries_ready_flag)
                ar_input = np.array(skeletons_ready[0])
                #print(f"ar_input.shape, {ar_input.shape} {ar_input[0,0,:]}")
                #print(f"ar_partition ar_new_idx,  {self.ar_partition} {self.ar_new_idx}")

                ar_input, ar_index_t = ar_get_data(ar_input, self.ar_partition, self.ar_new_idx)

                #print(ar_input.shape, ar_input[0,0,:])

                ids_ready_np = np.array(ids_ready)
                skeleton_timeseries_ready_flag_np = np.ones((1),dtype=np.int32)

                ar_inputs = []
                ar_input = np.expand_dims(ar_input, axis=0).astype(dtype=np.float32)

                ar_inputs.append(pb_utils.Tensor("input",ar_input))

                # Create inference request object
                infer_request = pb_utils.InferenceRequest(
                    model_name="ar",
                    requested_output_names=["pred_logits"],
                    inputs=ar_inputs,
                )

                # Perform synchronous blocking inference request
                infer_response = infer_request.exec()

                # Make sure that the inference response doesn't have an error. If
                # it has an error and you can't proceed with your model execution
                # you can raise an exception.
                if infer_response.has_error():
                    raise pb_utils.TritonModelException(infer_response.error().message())

                for output_tensor in infer_response.output_tensors():
                    if output_tensor.name() == "pred_logits":
                        pred_logits_pbTensor = output_tensor

                # Convert pb_utils.Tensor to DLPack tensor
                dlpack_tensor = pred_logits_pbTensor.to_dlpack()
                # Convert DLPack tensor to PyTorch tensor
                torch_tensor = from_dlpack(dlpack_tensor)
                # Move the tensor to CPU memory
                cpu_tensor = torch_tensor.cpu()
                pred_logits = cpu_tensor.detach().numpy()

                predicted_index = np.argmax(pred_logits)
                predicted_index_np = np.zeros((1),dtype=np.int32)
                predicted_index_np = predicted_index_np + predicted_index

                ar_confidence = pred_logits[0,predicted_index]
                ar_confidence_np = np.zeros((1),dtype=np.float32)
                ar_confidence_np = ar_confidence_np + ar_confidence
                action_predicted_flag_np = np.ones((1), dtype=np.int32)
            else:
                predicted_index_np = np.zeros((1),dtype=np.int32)
                predicted_index_np = predicted_index_np - 1
                ar_confidence_np = np.zeros((1),dtype=np.float32)
                action_predicted_flag_np = np.zeros((1), dtype=np.int32)


            out_tensor_1 = pb_utils.Tensor("predicted_index", predicted_index_np.astype(output1_dtype))
            out_tensor_2 = pb_utils.Tensor("ar_confidence", ar_confidence_np.astype(output2_dtype))
            out_tensor_3 = pb_utils.Tensor("action_predicted_flag", action_predicted_flag_np.astype(output3_dtype))


            #print(out_tensor_1)


            # Create InferenceResponse. You can set an error here in case
            # there was a problem with handling this inference request.
            # Below is an example of how you can set errors in inference
            # response:
            #
            # pb_utils.InferenceResponse(
            #    output_tensors=..., TritonError("An error occurred"))
            inference_response = pb_utils.InferenceResponse(
                output_tensors=[out_tensor_1, out_tensor_2, out_tensor_3]
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
