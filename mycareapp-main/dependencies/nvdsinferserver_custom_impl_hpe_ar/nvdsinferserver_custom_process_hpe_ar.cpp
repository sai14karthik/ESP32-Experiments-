/*
* SPDX-FileCopyrightText: Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
*/

#include <string.h>
#include <algorithm>
#include <iostream>
#include <map>
#include <mutex>
#include <numeric>
#include <sstream>
#include <string>
#include <unordered_map>
#include <google/protobuf/text_format.h>

#include "infer_custom_process.h"
#include "nvbufsurface.h"
#include "nvdsmeta.h"
#include "nvdsmeta_schema.h"
#include "nvdsinferserver_config.pb.h"

const std::string ACTION_LABELS[] = {
  "Lying Down",
  "Sitting",
  "Standing",
  "Walking"
};

typedef struct _GstBuffer GstBuffer;

/** This is a example how DeepStream Triton plugin(gst-nvinferserver) do
 * custom extra input preprocess and custom postprocess on triton based models.
 */

// enable debug log
#define ENABLE_DEBUG 0

namespace dsis = nvdsinferserver;

#if ENABLE_DEBUG
#define LOG_DEBUG(fmt, ...) fprintf(stdout, "%s:%d" fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__)
#else
#define LOG_DEBUG(fmt, ...)
#endif

#define LOG_ERROR(fmt, ...) fprintf(stderr, "%s:%d" fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__)

#ifndef INFER_ASSERT
#define INFER_ASSERT(expr)                                                     \
  do {                                                                       \
    if (!(expr)) {                                                         \
      fprintf(stderr, "%s:%d ASSERT(%s) \n", __FILE__, __LINE__, #expr); \
      std::abort();                                                      \
    }                                                                      \
  } while (0)
#endif
constexpr bool maintain_aspect_ratio = true;

#define CSTR(str) (str).empty() ? "" : (str).c_str()
#define MAX_TIME_STAMP_LEN 32

#define ACQUIRE_DISP_META(dmeta)  \
  if (dmeta->num_circles == MAX_ELEMENTS_IN_DISPLAY_META  || \
      dmeta->num_labels == MAX_ELEMENTS_IN_DISPLAY_META ||  \
      dmeta->num_lines == MAX_ELEMENTS_IN_DISPLAY_META) \
        { \
          nvds_add_display_meta_to_frame(frame_meta, dmeta);\
          dmeta = nvds_acquire_display_meta_from_pool(bmeta);\
        }\

#define GET_LINE(lparams) \
        ACQUIRE_DISP_META(dmeta)\
        lparams = &dmeta->line_params[dmeta->num_lines];\
        dmeta->num_lines++;\

static
void generate_ts_rfc3339 (char *buf, int buf_size)
{
  time_t tloc;
  struct tm tm_log;
  struct timespec ts;
  char strmsec[6]; //.nnnZ\0

  clock_gettime(CLOCK_REALTIME,  &ts);
  memcpy(&tloc, (void *)(&ts.tv_sec), sizeof(time_t));
  gmtime_r(&tloc, &tm_log);
  strftime(buf, buf_size,"%Y-%m-%dT%H:%M:%S", &tm_log);
  int ms = ts.tv_nsec/1000000;
  g_snprintf(strmsec, sizeof(strmsec),".%.3dZ", ms);
  strncat(buf, strmsec, buf_size);
}

static
gpointer copy_bodypose_meta (gpointer data, gpointer user_data)
{
  NvDsUserMeta *user_meta = (NvDsUserMeta *) data;
  NvDsEventMsgMeta *srcMeta = (NvDsEventMsgMeta *) user_meta->user_meta_data;
  NvDsEventMsgMeta *dstMeta = NULL;
  NvDsPersonObject *srcExt = NULL;
  NvDsPersonObject *dstExt = NULL;

  dstMeta = (NvDsEventMsgMeta *)g_memdup2 ((gpointer)srcMeta, sizeof(NvDsEventMsgMeta));

  // pose
  dstMeta->pose.num_joints = srcMeta->pose.num_joints;
  dstMeta->pose.pose_type = srcMeta->pose.pose_type;
  dstMeta->pose.joints = (NvDsJoint *)g_memdup2 ((gpointer)srcMeta->pose.joints,
                                    sizeof(NvDsJoint)*srcMeta->pose.num_joints);

  if (srcMeta->ts)
    dstMeta->ts = g_strdup (srcMeta->ts);

  if (srcMeta->sensorStr)
    dstMeta->sensorStr = g_strdup (srcMeta->sensorStr);

  if (srcMeta->objSignature.size > 0) {
    dstMeta->objSignature.signature = (gdouble *)g_memdup2 ((gpointer)srcMeta->objSignature.signature,
                                                sizeof(gdouble)*srcMeta->objSignature.size);
    dstMeta->objSignature.size = srcMeta->objSignature.size;
  }

  if(srcMeta->objectId) {
    dstMeta->objectId = g_strdup (srcMeta->objectId);
  }

  if (srcMeta->extMsg){
    dstMeta->extMsg = g_memdup2(srcMeta->extMsg, srcMeta->extMsgSize);
    dstMeta->extMsgSize = srcMeta->extMsgSize;
    srcExt = (NvDsPersonObject *)srcMeta->extMsg;
    dstExt = (NvDsPersonObject *)dstMeta->extMsg;
    dstExt->gender = g_strdup(srcExt->gender);
    dstExt->hair = g_strdup(srcExt->hair);
    dstExt->cap = g_strdup(srcExt->cap);
    dstExt->apparel = g_strdup(srcExt->apparel);
    dstExt->age = srcExt->age;
  }
  return dstMeta;
}

static void
release_bodypose_meta (gpointer data, gpointer user_data)
{
  NvDsUserMeta *user_meta = (NvDsUserMeta *) data;
  NvDsEventMsgMeta *srcMeta = (NvDsEventMsgMeta *) user_meta->user_meta_data;

  // pose
  g_free (srcMeta->pose.joints);
  g_free (srcMeta->ts);
  g_free (srcMeta->sensorStr);

  if (srcMeta->objSignature.size > 0) {
    g_free (srcMeta->objSignature.signature);
    srcMeta->objSignature.size = 0;
  }

  if(srcMeta->objectId) {
    g_free (srcMeta->objectId);
  }

  if(srcMeta->extMsg) {
    NvDsPersonObject *srcExt = (NvDsPersonObject *)srcMeta->extMsg;
    if(srcExt->gender) {
      g_free (srcExt->gender);
    }
    if(srcExt->hair) {
      g_free (srcExt->hair);
    }
    if(srcExt->cap) {
      g_free (srcExt->cap);
    }
    if(srcExt->apparel) {
      g_free (srcExt->apparel);
    }
    g_free (srcMeta->extMsg);
  }
  srcMeta->extMsgSize = 0;
  srcMeta->extMsg = NULL;

  g_free (user_meta->user_meta_data);
  user_meta->user_meta_data = NULL;
}

// constant values definition

/** Define a function for custom processor for DeepStream Triton plugin(nvinferserver)
 * do custom extra input preprocess and custom postprocess on triton based models.
 * The sysmbol is loaded through
 *   infer_config {
 *     custom_lib {  path: "path/to/custom_impl_process.so" }
 *     extra {
 *       custom_process_funcion: "CreateInferServerCustomProcess"
 *     }}
 */
extern "C" dsis::IInferCustomProcessor* CreateInferServerCustomProcessHPEAR(
    const char* config, uint32_t configLen);

namespace {
  using namespace dsis;

  std::string
    memType2Str(InferMemType t)
    {
      switch (t) {
        case InferMemType::kGpuCuda:
          return "kGpuCuda";
        case InferMemType::kCpu:
          return "kCpu";
        case InferMemType::kCpuCuda:
          return "kCpuPinned";
        default:
          return "Unknown";
      }
    }

  std::string
    dataType2Str(dsis::InferDataType t)
    {
      switch (t) {
        case InferDataType::kFp32:
          return "kFp32";
        case InferDataType::kFp16:
          return "kFp16";
        case InferDataType::kInt8:
          return "kInt8";
        case InferDataType::kInt32:
          return "kInt32";
        case InferDataType::kInt16:
          return "kInt16";
        case InferDataType::kUint8:
          return "kUint8";
        case InferDataType::kUint16:
          return "kUint16";
        case InferDataType::kUint32:
          return "kUint32";
        case InferDataType::kFp64:
          return "kFp64";
        case InferDataType::kInt64:
          return "kInt64";
        case InferDataType::kUint64:
          return "kUint64";
        case InferDataType::kString:
          return "kString";
        case InferDataType::kBool:
          return "kBool";
        default:
          return "Unknown";
      }
    }

  // return buffer description string
  std::string
    strOfBufDesc(const dsis::InferBufferDescription& desc)
    {
      std::stringstream ss;
      ss << "*" << desc.name << "*, shape: ";
      for (uint32_t i = 0; i < desc.dims.numDims; ++i) {
        if (i != 0) {
          ss << "x";
        } else {
          ss << "[";
        }
        ss << desc.dims.d[i];
        if (i == desc.dims.numDims - 1) {
          ss << "]";
        }
      }
      ss << ", dataType:" << dataType2Str(desc.dataType);
      ss << ", memType:" << memType2Str(desc.memType);
      return ss.str();
    }

}  // namespace

void splitString(std::string s, std::string delimiter, std::vector<std::string> &obj_vec) {
    size_t pos_start = 0, pos_end, delim_len = delimiter.length();
    std::string token;
    while ((pos_end = s.find(delimiter, pos_start)) != std::string::npos) {
        token = s.substr(pos_start, pos_end - pos_start);
        pos_start = pos_end + delim_len;
        //std::cout << token << std::endl;
        obj_vec.push_back(token);
    }
    //std::cout << s.substr(pos_start) << std::endl; // Last token
}

/** Example of a Custom process instance for deepstream-triton(gst-nvinferserver) plugin
 * It is derived from nvdsinferserver::IInferCustomProcessor
 * If should be loaded through
 * config_triton_inferserver_primary_fasterRCNN.txt:
 *   infer_config {
 *     custom_lib {  path: "path/to/custom_impl_process.so" }
 *     extra {
 *       custom_process_funcion: "CreateInferServerCustomProcess"
 *     }
 *   }
 */
class NvInferServerCustomProcessHPEAR : public dsis::IInferCustomProcessor {
  private:
    std::map<uint64_t, std::vector<float>> _streamFeedback;
    std::mutex _streamMutex;
    std::string _prompt_text;
    int _width = 1;
    int _height = 1;
    std::unordered_map<int,bool> _found_user_meta;
    std::unordered_map<int, std::vector<std::string>> _label_vector;
    nvdsinferserver::config::InferenceConfig infer_config;
    float threshold = 0.05;
    std::vector <std::string> mClassLabels;
    uint8_t start_frame = 1;
  public:

    NvInferServerCustomProcessHPEAR(){
      start_frame = 1;
    }
    NvInferServerCustomProcessHPEAR(const gchar *config){
      start_frame = 1;

    }
    ~NvInferServerCustomProcessHPEAR() override = default;
    /** override function
     * Specifies supported extraInputs memtype in extraInputProcess()
     */
    void supportInputMemType(dsis::InferMemType& type) override { type = dsis::InferMemType::kCpu; }

    /** override function
     * check whether custom loop process needed.
     * If return True, extraInputProcess() and inferenceDone() runs in order per stream_ids
     * This is usually for LSTM loop purpose. FasterRCNN does not need it.
     * The code for requireInferLoop() conditions just sample when user has
     * a LSTM-like Loop model and requires loop custom processing.
     * */
    bool requireInferLoop() const override { return false; }

    /**
     * override function
     * Do custom processing on extra inputs.
     * @primaryInput is already preprocessed. DO NOT update it again.
     * @extraInputs, do custom processing and fill all data according the tensor shape
     * @options, it has most of the common Deepstream metadata along with primary data.
     *           e.g. NvDsBatchMeta, NvDsObjectMeta, NvDsFrameMeta, stream ids...
     *           see infer_ioptions.h to see all the potential key name and structures
     *           in the key-value table.
     */
    NvDsInferStatus extraInputProcess(
        const std::vector<dsis::IBatchBuffer*>&
        primaryInputs,  // primary tensor(image) has been processed
        std::vector<dsis::IBatchBuffer*>& extraInputs, const dsis::IOptions* options) override
    {
      INFER_ASSERT(primaryInputs.size() > 0);
      //INFER_ASSERT(extraInputs.size() == 1);
      // primary input tensor: input_1 [batch, channel, height, width]
      dsis::InferBufferDescription input0Desc = primaryInputs[0]->getBufDesc();
      // extra input tensor: image_shape [batch, 2]
      dsis::InferBufferDescription extra1Desc = extraInputs[0]->getBufDesc();
      INFER_ASSERT(extra1Desc.dataType == dsis::InferDataType::kFp32);
      INFER_ASSERT(extra1Desc.elementSize == sizeof(float));  // bytes per element

      dsis::InferBufferDescription extra2Desc = extraInputs[1]->getBufDesc();
      INFER_ASSERT(extra2Desc.dataType == dsis::InferDataType::kInt32);
      INFER_ASSERT(extra2Desc.elementSize == sizeof(int32_t));  // bytes per element

      INFER_ASSERT(!strOfBufDesc(input0Desc).empty());
      LOG_DEBUG("extraInputProcess: primary input %s", strOfBufDesc(input0Desc).c_str());
      LOG_DEBUG("extraInputProcess: extra input %s", strOfBufDesc(extra1Desc).c_str());
      LOG_DEBUG("extraInputProcess: extra input %s", strOfBufDesc(extra2Desc).c_str());

      // batch size must be get from primary input tensor.
      // extra inputs 'image_shape' does not have a batch size in this specific model
      _height = input0Desc.dims.d[2];
      _width = input0Desc.dims.d[3];

      int data_size = extra1Desc.dims.d[1];
      //INFER_ASSERT(extra1Desc.dims.numDims == 2 && extra1Desc.dims.d[0] == batchSize);
      //INFER_ASSERT(batchSize >= 1);

      NvDsBatchMeta* batchMeta = nullptr;
      std::vector<NvDsFrameMeta*> frameMetaList;
      NvBufSurface* bufSurf = nullptr;
      std::vector<NvBufSurfaceParams*> surfParamsList;
      int64_t unique_id = 0;

      // get NvBufSurface
      if (options->hasValue(OPTION_NVDS_BUF_SURFACE)) {
        INFER_ASSERT(options->getObj(OPTION_NVDS_BUF_SURFACE, bufSurf) == NVDSINFER_SUCCESS);
      }
      INFER_ASSERT(bufSurf);

      // get NvDsBatchMeta
      if (options->hasValue(OPTION_NVDS_BATCH_META)) {
        INFER_ASSERT(options->getObj(OPTION_NVDS_BATCH_META, batchMeta) == NVDSINFER_SUCCESS);
      }
      INFER_ASSERT(batchMeta);

      // get all frame meta list into vector<NvDsFrameMeta*>
      if (options->hasValue(OPTION_NVDS_FRAME_META_LIST)) {
        INFER_ASSERT(
            options->getValueArray(OPTION_NVDS_FRAME_META_LIST, frameMetaList) ==
            NVDSINFER_SUCCESS);
      }

      // get unique_id
      if (options->hasValue(OPTION_NVDS_UNIQUE_ID)) {
        INFER_ASSERT(options->getInt(OPTION_NVDS_UNIQUE_ID, unique_id) == NVDSINFER_SUCCESS);
      }

      // get all surface params list into vector<NvBufSurfaceParams*>
      if (options->hasValue(OPTION_NVDS_BUF_SURFACE_PARAMS_LIST)) {
        INFER_ASSERT(
            options->getValueArray(OPTION_NVDS_BUF_SURFACE_PARAMS_LIST, surfParamsList) ==
            NVDSINFER_SUCCESS);
      }

      NvDsMetaList *l_frame = NULL;
      NvDsMetaList *l_objmeta = NULL;

      float* bbox = (float*)extraInputs[0]->getBufPtr(0);
      int32_t* tracker_id = (int32_t*)extraInputs[1]->getBufPtr(0);
      int32_t cnt = 0;

      for (l_frame = batchMeta->frame_meta_list; l_frame != NULL;
          l_frame = l_frame->next)
      {
        NvDsFrameMeta *frame_meta = (NvDsFrameMeta *)(l_frame->data);
        for (l_objmeta = frame_meta->obj_meta_list; l_objmeta != NULL;
            l_objmeta = l_objmeta->next)
        {
            NvDsObjectMeta *obj_meta = (NvDsObjectMeta *)(l_objmeta->data);
            bbox[cnt*4+1] = obj_meta->rect_params.left;
            bbox[cnt*4 + 2] = obj_meta->rect_params.top;
            bbox[cnt*4 + 3] = obj_meta->rect_params.width;
            bbox[cnt*4 + 4] = obj_meta->rect_params.height;
            tracker_id[cnt+1] = obj_meta->object_id;
            cnt++;
        }
      }
      bbox[0] = cnt;
      tracker_id[0] = cnt;

      uint8_t* start_frame_ptr = (uint8_t*)extraInputs[2]->getBufPtr(0);
      start_frame_ptr[0] = start_frame;
      if (start_frame == 1){
        start_frame = 0;
      }


      return NVDSINFER_SUCCESS;
    }

    /** override function
     * Custom processing for inferenced output tensors.
     * output memory types is controlled by gst-nvinferserver config file
     *     config_triton_inferserver_primary_fasterRCNN.txt:
     *       infer_config {
     *         backend {  output_mem_type: MEMORY_TYPE_CPU }
     *     }
     * User can even attach parsed metadata into GstBuffer from this function
     */
    NvDsInferStatus inferenceDone(
        const dsis::IBatchArray* outputs, const dsis::IOptions* inOptions) override
    {
      std::vector<uint64_t> streamIds;
      // InferBufferDescription input0Desc = primaryInputs[0]->getBufDesc();
      INFER_ASSERT(
          inOptions->getValueArray(OPTION_NVDS_SREAM_IDS, streamIds) == NVDSINFER_SUCCESS);
      INFER_ASSERT(!streamIds.empty());

      // get NvDsBatchMeta
      NvDsBatchMeta* batchMeta = nullptr;
      if (inOptions->hasValue(OPTION_NVDS_BATCH_META)) {
        INFER_ASSERT(inOptions->getObj(OPTION_NVDS_BATCH_META, batchMeta) == NVDSINFER_SUCCESS);
      }
      INFER_ASSERT(batchMeta);
      uint32_t batchSize = streamIds.size();

      NvBufSurface* bufSurf = nullptr;
      std::vector<NvBufSurfaceParams*> surfParamsList;

      // add 3 output tensors into map
      std::unordered_map<std::string, const dsis::IBatchBuffer*> tensors;
      for (uint32_t iTensor = 0; iTensor < outputs->getSize(); ++iTensor) {
        const dsis::IBatchBuffer* outTensor = outputs->getBuffer(iTensor);
        INFER_ASSERT(outTensor);
        const auto& desc = outTensor->getBufDesc();
        LOG_DEBUG("out tensor: %s, desc: %s", CSTR(desc.name), strOfBufDesc(desc).c_str());
        tensors.emplace(desc.name, outTensor);
      }

      auto poses = tensors["poses"];
      auto bbox = tensors["bbox"];
      auto tracked_id = tensors["tracked_id"];
      auto action_predicted_flag = tensors["action_predicted_flag"];
      auto predicted_index = tensors["predicted_index"];
      auto ar_confidence = tensors["ar_confidence"];
      INFER_ASSERT(poses && bbox && tracked_id);
      auto posesDesc = poses->getBufDesc();
      auto bboxDesc = bbox->getBufDesc();
      auto tracked_idDesc = tracked_id->getBufDesc();
      auto action_predicted_flagDesc = action_predicted_flag->getBufDesc();
      auto predicted_indexDesc = predicted_index->getBufDesc();
      auto ar_confidenceDesc = ar_confidence->getBufDesc();

      float* posesPtr = (float*)poses->getBufPtr(0);
      float* bboxPtr = (float*)bbox->getBufPtr(0);
      int32_t* trackerIdPtr = (int32_t*)tracked_id->getBufPtr(0);
      int32_t trackedIdsize = trackerIdPtr[0];
      int32_t* action_predicted_flagPtr = (int32_t*)action_predicted_flag->getBufPtr(0);
      int32_t* predicted_indexPtr = (int32_t*)predicted_index->getBufPtr(0);
      float* ar_confidencePtr = (float*)ar_confidence->getBufPtr(0);

      if (inOptions->hasValue(OPTION_NVDS_BUF_SURFACE)) {
        INFER_ASSERT(inOptions->getObj(OPTION_NVDS_BUF_SURFACE, bufSurf) == NVDSINFER_SUCCESS);
      }
      INFER_ASSERT(bufSurf);
      // get all surface params list into vector<NvBufSurfaceParams*>
      if (inOptions->hasValue(OPTION_NVDS_BUF_SURFACE_PARAMS_LIST)) {
        INFER_ASSERT(
            inOptions->getValueArray(OPTION_NVDS_BUF_SURFACE_PARAMS_LIST, surfParamsList) ==
            NVDSINFER_SUCCESS);
      }

      NvDsMetaList *l_frame = NULL;
      NvDsMetaList *l_objmeta = NULL;

      for (l_frame = batchMeta->frame_meta_list; l_frame != NULL;
          l_frame = l_frame->next)
			{
				NvDsFrameMeta *frame_meta = (NvDsFrameMeta *)(l_frame->data);
				for (l_objmeta = frame_meta->obj_meta_list; l_objmeta != NULL;
						l_objmeta = l_objmeta->next)
				{
					NvDsObjectMeta *obj_meta = (NvDsObjectMeta *)(l_objmeta->data);
					int32_t tracking_id = obj_meta->object_id;
					int32_t loc_idx = -1;
					for (int i = 0; i < trackedIdsize; ++i){
						if (tracking_id == trackerIdPtr[i+1]){
							loc_idx = i;
							break;
						}
					}
					if (loc_idx == -1){
						continue;
					}

					NvDsEventMsgMeta *msg_meta = (NvDsEventMsgMeta *) g_malloc0 (sizeof (NvDsEventMsgMeta));
					NvDsPersonObject *msg_meta_ext = (NvDsPersonObject *) g_malloc0 (sizeof (NvDsPersonObject));
					NvDsBatchMeta *bmeta = frame_meta->base_meta.batch_meta;
					nvds_acquire_meta_lock (bmeta);
					NvDsDisplayMeta *dmeta = nvds_acquire_display_meta_from_pool(bmeta);
					const int numKeyPoints = 18;
					float keypoints[3* numKeyPoints];
					for (int i = 0; i < numKeyPoints; ++i){
						keypoints[3*i] = posesPtr[loc_idx*numKeyPoints*3   + i*3];
						keypoints[3*i+1] = posesPtr[loc_idx*numKeyPoints*3 + i*3+1];
						keypoints[3*i+2] = posesPtr[loc_idx*numKeyPoints*3 + i*3+2];
					}


					msg_meta->type = NVDS_EVENT_ENTRY; //Should this be ENTRY
					msg_meta->objType = (NvDsObjectType) NVDS_OBJECT_TYPE_PERSON;
					msg_meta->bbox.left = bboxPtr[loc_idx*4+1];
					msg_meta->bbox.top = bboxPtr[loc_idx*4+2];
					msg_meta->bbox.width = bboxPtr[loc_idx*4+3];
					msg_meta->bbox.height = bboxPtr[loc_idx*4+4];
					msg_meta->extMsg = msg_meta_ext;
					msg_meta->extMsgSize  = sizeof(NvDsPersonObject);
					msg_meta_ext->gender  = nullptr;
					msg_meta_ext->hair    = nullptr;
					msg_meta_ext->cap     = nullptr;
					msg_meta_ext->apparel = nullptr;
					msg_meta_ext->age = 0;

					msg_meta->pose.pose_type = 3;// posePCT

					msg_meta->pose.num_joints = numKeyPoints;
					msg_meta->pose.joints = (NvDsJoint *)g_malloc0(sizeof(NvDsJoint) * numKeyPoints);
					for(int i = 0; i < msg_meta->pose.num_joints; i++){
						msg_meta->pose.joints[i].x = keypoints[3*i  ];
						msg_meta->pose.joints[i].y = keypoints[3*i+1];
						msg_meta->pose.joints[i].z = keypoints[3*i+2];
            /*Confidence output is not available for each keypoint from the model */
            msg_meta->pose.joints[i].confidence = 1.0;
					}

					msg_meta->objectId = (gchar *) g_malloc0 (MAX_LABEL_SIZE);
					std::string action_label = "";
					if (obj_meta){
						msg_meta->objClassId = obj_meta->class_id;
						msg_meta->trackingId = obj_meta->object_id;
            if (action_predicted_flagPtr[loc_idx] == 1){
              action_label =
                             ACTION_LABELS[predicted_indexPtr[loc_idx]] + " " +
                             std::to_string(ar_confidencePtr[loc_idx]);
						  strncpy(msg_meta->objectId, action_label.c_str(), MAX_LABEL_SIZE);
              /* Repurpose gender for sending the detected person class */
              msg_meta_ext->gender = g_strdup(obj_meta->obj_label);
						  //strncpy(obj_meta->obj_label, action_label.c_str(), MAX_LABEL_SIZE);
            }
					}
					else {
						msg_meta->objClassId = 0;
						msg_meta->trackingId = 0;
						strncpy(msg_meta->objectId, "Person", MAX_LABEL_SIZE);
					}
					msg_meta->frameId = frame_meta->frame_num;
					msg_meta->confidence = 1.0;
					msg_meta->ts = (gchar *) g_malloc0 (MAX_TIME_STAMP_LEN + 1);
					generate_ts_rfc3339(msg_meta->ts, MAX_TIME_STAMP_LEN);

					osdBody(frame_meta, bmeta, dmeta, numKeyPoints,
							keypoints, action_label.c_str(),
              msg_meta->bbox.left, msg_meta->bbox.top+30);
					NvDsUserMeta *user_event_meta = nvds_acquire_user_meta_from_pool (bmeta);
					if (user_event_meta) {
						user_event_meta->user_meta_data = (void *) msg_meta;
						user_event_meta->base_meta.meta_type = NVDS_EVENT_MSG_META;
						user_event_meta->base_meta.copy_func = (NvDsMetaCopyFunc) copy_bodypose_meta;
						user_event_meta->base_meta.release_func = (NvDsMetaReleaseFunc)release_bodypose_meta;
						nvds_add_user_meta_to_frame(frame_meta, user_event_meta);
					} else {
						g_printerr("Error in attaching event meta to buffer\n");
					}
					nvds_release_meta_lock (bmeta);

				}
			}


      return NVDSINFER_SUCCESS;
    }

  void osdBody(NvDsFrameMeta* frame_meta,
      NvDsBatchMeta *bmeta,
      NvDsDisplayMeta *dmeta,
      const int numKeyPoints,
      const float keypoints[],
      std::string action_label,
      int x = 20,
      int y = 20)
{
  const int keypoint_radius = 3;//6;//3;
  const int keypoint_line_width = 2;//4;//2;

  const int num_joints = numKeyPoints;

  // RPM Joints: 0: base of spine, 1: middle of spine, 2: neck,
  //             3: left shoulder, 4: left elbow, 5: left wrist,
  //             6: right shoulder, 7: right elbow, 8: right wrist,
  //             9: left hip, 10: left knee, 11: left ankle,
  //             12: right hip, 13: right knee, 14: right ankle,
  //             15: nose, 16: left ear, 17: right ear

  const int idx_bones[] = { 3, 4, 4, 5, //left arm
                            6, 7, 7, 8, //right arm
                            9, 10, 10, 11, //left leg
                            12,13, 13, 14, //right leg

                            9, 12,  3, 6, //hip and shoulder
                            0, 1,   1, 2, //spine neck
                            15, 16, 16, 17, 17, 15, 15, 2
                            };
  const NvOSD_ColorParams bone_colors[] = {
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{0, 0.1, 0.1, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1},
                          NvOSD_ColorParams{0, 0, 1.0, 1},
                          NvOSD_ColorParams{1.0, 0, 0, 1},
                          NvOSD_ColorParams{0, 1.0, 0, 1}};

  for (int ii = 0; ii < num_joints; ii++) {
    int i = ii;// idx_joints[ii];


    ACQUIRE_DISP_META(dmeta);
    NvOSD_CircleParams &cparams = dmeta->circle_params[dmeta->num_circles];
    cparams.xc = keypoints[3 * i    ];
    cparams.yc = keypoints[3 * i + 1];
    cparams.radius = keypoint_radius;
    cparams.circle_color = NvOSD_ColorParams{1.0, 0, 0, 1};
    cparams.has_bg_color = 1;
    cparams.bg_color = NvOSD_ColorParams{1.0, 0, 0, 1};
    dmeta->num_circles++;
    //std::cout << "Display meta num_circles: " << dmeta->num_circles << std::endl;
  }

  for (long unsigned int i = 0; i < sizeof(idx_bones)/(2*sizeof(idx_bones[0])); i++) {
    int i0 = idx_bones[2 * i    ];
    int i1 = idx_bones[2 * i + 1];

    ACQUIRE_DISP_META(dmeta);
    NvOSD_LineParams *lparams = &dmeta->line_params[dmeta->num_lines];
    lparams->x1 = keypoints[3 * i0];
    lparams->y1 = keypoints[3 * i0 + 1];
    lparams->x2 = keypoints[3 * i1];
    lparams->y2 = keypoints[3 * i1 + 1];
    lparams->line_width = keypoint_line_width;
    lparams->line_color = bone_colors[i];
    dmeta->num_lines++;
    //std::cout << "Display meta num_lines: " << dmeta->num_lines << std::endl;
  }

  if (action_label != "") {
    //ACQUIRE_DISP_META(dmeta);
    NvOSD_TextParams *tparams = &dmeta->text_params[dmeta->num_labels];
    tparams->x_offset = x;
    tparams->y_offset = y;
    tparams->display_text = g_strdup_printf("%s", action_label.c_str());
    //printf("Display meta action_label: %s\n", action_label.c_str());
    /* Set black background for the text. */
    tparams->set_bg_clr = 1;
    tparams->text_bg_clr = (NvOSD_ColorParams){0, 0, 0, 1};
    tparams->font_params.font_color = (NvOSD_ColorParams){1, 1, 1, 1};
    /* Font face, size and color. */
    tparams->font_params.font_name = (gchar*)"Serif";
    tparams->font_params.font_size = 11;
    dmeta->num_labels++;
    //std::cout << "Display meta action_label: " << action_label << std::endl;
  }

  nvds_add_display_meta_to_frame(frame_meta, dmeta);
#if 0
   NvDsMetaList *display_meta_list = NULL, *l;
   display_meta_list = bmeta->display_meta_pool->full_list;
   NvDsDisplayMeta *display_meta = NULL;

   /* Get objects to be drawn from display meta.
    * Draw objects if count equals MAX_OSD_ELEMS.
    */
  int dispmetacount = 0;
   for (l = display_meta_list; l != NULL; l = l->next) {
     display_meta = (NvDsDisplayMeta *) (l->data);
     dispmetacount++;

    std::cout << "********************* bmeta: " << bmeta << std::endl;
    std::cout << "**Custom lib Display meta count: " << dispmetacount << std::endl;
    std::cout << "Display meta num_rects: " << display_meta->num_rects << std::endl;
    std::cout << "Display meta num_labels: " << display_meta->num_labels << std::endl;
    if(display_meta->num_labels > 0) {
      std::cout << "Display meta text_params[0].display_text: " << display_meta->text_params[0].display_text << std::endl;
    }
    std::cout << "Display meta num_lines: " << display_meta->num_lines << std::endl;
    std::cout << "Display meta num_arrows: " << display_meta->num_arrows << std::endl;
    std::cout << "Display meta num_circles: " << display_meta->num_circles << std::endl;
    std::cout << "*********************" << std::endl;

   }
#endif

  return;
}


    /** override function
     * Receiving errors if anything wrong inside lowlevel lib
     */
    void notifyError(NvDsInferStatus s) override
    {
      std::unique_lock<std::mutex> locker(_streamMutex);
      _streamFeedback.clear();
    }

  private:
    /** function for loop processing only. not requried for fasterRCNN
     */
    NvDsInferStatus feedbackStreamInput(
        const dsis::IBatchArray* outputs, const dsis::IOptions* inOptions);

};

/** Implementation to Create a custom processor for DeepStream Triton
 * plugin(nvinferserver) to do custom extra input preprocess and custom
 * postprocess on triton based models.
 */
extern "C" {
  dsis::IInferCustomProcessor*
    CreateInferServerCustomProcessHPEAR(const char* config, uint32_t configLen)
    {

        return new NvInferServerCustomProcessHPEAR(config);

    }
}
