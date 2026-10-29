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
#include "nvdsinferserver_config.pb.h"


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

// constant values definition
#if 0
static const std::vector<std::string> kClassLabels = {
  "person",        "bicycle",       "car",           "motorbike",
  "aeroplane",     "bus",           "train",         "truck",
  "boat",          "traffic light", "fire hydrant",  "stop sign",
  "parking meter", "bench",         "bird",          "cat",
  "dog",           "horse",         "sheep",         "cow",
  "elephant",      "bear",          "zebra",         "giraffe",
  "backpack",      "umbrella",      "handbag",       "tie",
  "suitcase",      "frisbee",       "skis",          "snowboard",
  "sports ball",   "kite",          "baseball bat",  "baseball glove",
  "skateboard",    "surfboard",     "tennis racket", "bottle",
  "wine glass",    "cup",           "fork",          "knife",
  "spoon",         "bowl",          "banana",        "apple",
  "sandwich",      "orange",        "broccoli",      "carrot",
  "hot dog",       "pizza",         "donut",         "cake",
  "chair",         "sofa",          "pottedplant",   "bed",
  "diningtable",   "toilet",        "tvmonitor",     "laptop",
  "mouse",         "remote",        "keyboard",      "cell phone",
  "microwave",     "oven",          "toaster",       "sink",
  "refrigerator",  "book",          "clock",         "vase",
  "scissors",      "teddy bear",    "hair drier",    "toothbrush",
  "building",      "road sign"
};

#else

static const std::vector<std::string> kClassLabels = {
//"person" , "bicycle", "car", "bus",  "truck", "bicycle",  "backpack"
"patient" , "visitor", "doctor", "nurse"
};
#endif


/** Define a function for custom processor for DeepStream Triton plugin(nvinferserver)
 * do custom extra input preprocess and custom postprocess on triton based models.
 * The sysmbol is loaded through
 *   infer_config {
 *     custom_lib {  path: "path/to/custom_impl_process.so" }
 *     extra {
 *       custom_process_funcion: "CreateInferServerCustomProcess"
 *     }}
 */
extern "C" dsis::IInferCustomProcessor* CreateInferServerCustomProcess(
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
class NvInferServerCustomProcess : public dsis::IInferCustomProcessor {
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
  public:

    NvInferServerCustomProcess(){
      for (std::string elem: kClassLabels){
        _prompt_text = _prompt_text + elem + " . ";
        mClassLabels.push_back (elem);

      }

    }
    NvInferServerCustomProcess(const gchar *config){

      std::string config_str(config);
      if(!google::protobuf::TextFormat::ParseFromString(config_str, &infer_config)) {
        std::cout << "Failed Parsing " << config_str << std::endl;
        for (std::string elem: kClassLabels){
          _prompt_text = _prompt_text + elem + " . ";
          mClassLabels.push_back (elem);

        }
      }
      else {
        std::vector<std::string> tokens;
        std::string token;
        std::stringstream ss (infer_config.postprocess().other().type_name());
        while (getline(ss, token, ';')) {
          tokens.push_back(token);
          std::cout << "tokens " << token << std::endl;
        }
        if (tokens.size() == 2){
          threshold = std::stof(tokens[1]);
        }
        if (tokens.size() >= 1){
          mClassLabels.clear();
          std::string delim = " . ";
          std::string s=tokens[0];

          auto start = 0U;
          auto end = s.find(delim);
          std::string elem;

          _prompt_text = "";
          while (end != std::string::npos)
          {
            elem=s.substr(start, end - start);
            mClassLabels.push_back(elem);
            std::cout << elem << std::endl;
            _prompt_text = _prompt_text + elem + " . ";
            start = end + delim.length();
            end = s.find(delim, start);
            elem.erase(0, elem.find_first_not_of(" \n\r\t"));
            elem.erase(elem.find_last_not_of(" \n\r\t") + 1);

          }

          elem=s.substr(start, end - start);
          elem.erase(0, elem.find_first_not_of(" \n\r\t"));
          elem.erase(elem.find_last_not_of(" \n\r\t") + 1);
          mClassLabels.push_back(elem);
          _prompt_text = _prompt_text + elem + " . ";
          std::cout << elem << "\n" << _prompt_text << std::endl;
        }
      }

     //std::cout << "Default prompt: " << _prompt_text << std::endl;

    }
    ~NvInferServerCustomProcess() override = default;
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
      //return NVDSINFER_SUCCESS;
      INFER_ASSERT(primaryInputs.size() > 0);
      //INFER_ASSERT(extraInputs.size() == 1);
      // primary input tensor: input_1 [batch, channel, height, width]
      dsis::InferBufferDescription input0Desc = primaryInputs[0]->getBufDesc();
      // extra input tensor: image_shape [batch, 2]
      dsis::InferBufferDescription extra1Desc = extraInputs[0]->getBufDesc();
      INFER_ASSERT(extra1Desc.dataType == dsis::InferDataType::kUint8);
      INFER_ASSERT(extra1Desc.elementSize == sizeof(char));  // bytes per element
                                                             //

      INFER_ASSERT(!strOfBufDesc(input0Desc).empty());
      LOG_DEBUG("extraInputProcess: primary input %s", strOfBufDesc(input0Desc).c_str());
      LOG_DEBUG("extraInputProcess: extra input %s", strOfBufDesc(extra1Desc).c_str());
      //printf("extraInputProcess: extra input %s\n", strOfBufDesc(extra1Desc).c_str());

      // batch size must be get from primary input tensor.
      // extra inputs 'image_shape' does not have a batch size in this specific model
      _height = input0Desc.dims.d[2];
      _width = input0Desc.dims.d[3];
      //_height = input0Desc.dims.d[1];
      //_width = input0Desc.dims.d[2]; 
      //printf ("%dx%d\n",_width,_height);

      int data_size = extra1Desc.dims.d[1];
      //INFER_ASSERT(extra1Desc.dims.numDims == 2 && extra1Desc.dims.d[0] == batchSize);
      //INFER_ASSERT(batchSize >= 1);
      //if (!options) {
      //    LOG_ERROR("custom process does not receive IOptions");
      //    return NVDSINFER_CUSTOM_LIB_FAILED;
      //}

      NvDsBatchMeta* batchMeta = nullptr;
      std::vector<NvDsFrameMeta*> frameMetaList;
      NvBufSurface* bufSurf = nullptr;
      std::vector<NvBufSurfaceParams*> surfParamsList;
      //std::vector<uint64_t> streamIds;
      int64_t unique_id = 0;
      //INFER_ASSERT(options->getValueArray(OPTION_NVDS_SREAM_IDS, streamIds) == NVDSINFER_SUCCESS);
      //INFER_ASSERT(streamIds.size() == (uint32_t)batchSize);

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

      // fill extra input tensor "image_shape[-1,2]"
      /*
         float* image_shape = (float*)extraInputs[0]->getBufPtr(0);

         for (int iBatch = 0; iBatch < batchSize; ++iBatch) {
         image_shape[iBatch * 2 + 1] = (float)surfParamsList[iBatch]->width;
         image_shape[iBatch * 2] = (float)surfParamsList[iBatch]->height;
         }
       */

      NvDsMetaList *l_frame = NULL;
      NvDsMetaList *l_user = NULL;

      for (l_frame = batchMeta->frame_meta_list; l_frame != NULL;
          l_frame = l_frame->next)
      {
        NvDsFrameMeta *frame_meta = (NvDsFrameMeta *)(l_frame->data);

        _found_user_meta [frame_meta->source_id] = false;
        for (l_user = frame_meta->frame_user_meta_list; l_user != NULL;
            l_user = l_user->next)
        {

          NvDsUserMeta *user_meta = (NvDsUserMeta *)l_user->data;

          if (user_meta->base_meta.meta_type == (NvDsMetaType)(NVDS_START_USER_META + 2))
          {
            gchar *prompt =
              (gchar *)user_meta->user_meta_data;
            char * string_ptr = (char*)extraInputs[0]->getBufPtr(0) +
              frame_meta->batch_id*data_size;
            std::vector<std::string> obj_vec;

            strncpy(string_ptr, prompt, data_size);
            //printf("%s -> %p ->%s = %d\n",prompt, string_ptr, string_ptr, frame_meta->batch_id);
            _found_user_meta [frame_meta->source_id] = true;
            splitString(prompt, " . ", obj_vec);
            _label_vector[frame_meta->source_id] = std::move(obj_vec);
          }

        }
      } 

      for (auto &elem: _found_user_meta){
        if (elem.second == false){
            char * string_ptr = (char*)extraInputs[0]->getBufPtr(0) +elem.first*data_size;
            strncpy(string_ptr, _prompt_text.c_str(), data_size);
            //std::cout << _prompt_text << std::endl;
        }
      }


      //printf ("%p, %d,\n", string_ptr, data_size);
      return NVDSINFER_SUCCESS;
    }

    std::vector<NvDsInferObjectDetectionInfo> nonMaximumSuppression(
    const std::vector<NvDsInferObjectDetectionInfo>& bbox, const float nmsThreshold)
    {
      auto overlap1D = [](float x1min, float x1max, float x2min,
          float x2max) -> float {
        if (x1min > x2min) {
          std::swap(x1min, x2min);
          std::swap(x1max, x2max);
        }
        return x1max < x2min ? 0 : std::min(x1max, x2max) - x2min;
      };

      auto computeIoU = [&overlap1D](
          const NvDsInferObjectDetectionInfo& bbox1,
          const NvDsInferObjectDetectionInfo& bbox2) -> float {
        float overlapX = overlap1D(
            bbox1.left, bbox1.left + bbox1.width, bbox2.left,
            bbox2.left + bbox2.width);
        float overlapY = overlap1D(
            bbox1.top, bbox1.top + bbox1.height, bbox2.top,
            bbox2.top + bbox2.height);
        float area1 = (bbox1.width) * (bbox1.height);
        float area2 = (bbox2.width) * (bbox2.height);
        float overlap2D = overlapX * overlapY;
        float u = area1 + area2 - overlap2D;
        return u == 0 ? 0 : overlap2D / u;
      };

      std::vector<NvDsInferObjectDetectionInfo> output;
      for (unsigned int idx = 0; idx < bbox.size(); idx++) {
        bool keep = true;
        for (unsigned k = 0; k < output.size(); ++k) {
          if (keep) {
            float overlap = computeIoU(bbox.at(idx), output.at(k));
            //LOG_DEBUG("i = %d, k = %d, overlap = %f", idx, k, overlap);
            keep = overlap <= nmsThreshold;
          } else {
            break;
          }
        }
        if (keep) {
          output.push_back(bbox[idx]);
        }
      }
      return output;
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
      uint32_t batchSize = streamIds.size();
      std::vector<std::vector<NvDsInferObjectDetectionInfo>> parsedBboxes(batchSize);

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

      // indices dim format is [total, 3], each row represents [batch_index, class_index,
      // box_index]
      auto indices = tensors["labels"];
      // boxes dim format is [batch, n_candidates, 4]
      auto boxes = tensors["boxes"];
      // scores dim format is [1, n_classes, n_candidates]
      auto scores = tensors["scores"];
      INFER_ASSERT(indices && boxes && scores);
      auto idxDesc = indices->getBufDesc();
      auto boxDesc = boxes->getBufDesc();
      auto scoreDesc = scores->getBufDesc();
      //INFER_ASSERT(idxDesc.dims.numDims == 2 && idxDesc.dims.d[1] == 3);
      //INFER_ASSERT(boxDesc.dims.numDims == 3 && boxDesc.dims.d[2] == 4);
      //INFER_ASSERT(
      //   scoreDesc.dims.numDims == 3 && scoreDesc.dims.d[1] == (int)kClassLabels.size());
      int32_t* indicesPtr = (int32_t*)indices->getBufPtr(0);
      float* boxesPtr = (float*)boxes->getBufPtr(0);
      float* scoresPtr = (float*)scores->getBufPtr(0);
      //printf ("Parser %d %d %d\n", idxDesc.dims.numDims, boxDesc.dims.numDims, scoreDesc.dims.numDims);
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



      for (int idx = 0; idx < idxDesc.dims.d[0]; ++idx) {
        float scalex = (float)surfParamsList[idx]->width/_width;
        float scaley = (float)surfParamsList[idx]->height/_height;
        if (maintain_aspect_ratio){
          if (scalex > scaley){
            scaley = scalex;
          }
          else{
            scalex = scaley;
          }
        }

        for (int i = 0; i < idxDesc.dims.d[1]; ++i){
          NvDsInferObjectDetectionInfo obj;
          if (scoresPtr[i + idx*idxDesc.dims.d[1]] < 0.01){
            continue;
          }
          obj.classId = (uint32_t)indicesPtr[i + idx*idxDesc.dims.d[1]];
          float* bbox = &boxesPtr[4 * i + idx*idxDesc.dims.d[1]*4];
          obj.left = bbox[0]*scalex;
          obj.top = bbox[1]*scaley;
          float right = bbox[2]*scalex;
          float bottom = bbox[3]*scaley;
          obj.height = bottom - obj.top;
          obj.width = right - obj.left;
          float score = scoresPtr[i + idx*idxDesc.dims.d[1]];
          obj.detectionConfidence = score;
          //printf(
          //		"cid: %u, n: %u, obj [%.2f, %.2f, %.2f, %.2f], score:%.2f\n", obj.classId, idx,
          //		obj.top, obj.left, obj.width, obj.height, score);
          parsedBboxes[idx].emplace_back(obj);
        }
      }

      for (uint32_t iBatch = 0; iBatch < batchSize; ++iBatch) {
        INFER_ASSERT(attachObjMeta(inOptions, parsedBboxes[iBatch], iBatch) == NVDSINFER_SUCCESS);
      }
      return NVDSINFER_SUCCESS;
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

    /**
     * attach bounding boxes into NvDsBatchMeta and NvDsFrameMeta
     */
    NvDsInferStatus attachObjMeta(
        const dsis::IOptions* inOptions, const std::vector<NvDsInferObjectDetectionInfo>& objs,
        uint32_t batchIdx);
};

/** Implementation to Create a custom processor for DeepStream Triton
 * plugin(nvinferserver) to do custom extra input preprocess and custom
 * postprocess on triton based models.
 */
extern "C" {
  dsis::IInferCustomProcessor*
    CreateInferServerCustomProcess(const char* config, uint32_t configLen)
    {

      return new NvInferServerCustomProcess(config);

    }
}

/**
 * attach bounding boxes into NvDsBatchMeta and NvDsFrameMeta
 */
  NvDsInferStatus
NvInferServerCustomProcess::attachObjMeta(
    const dsis::IOptions* inOptions, const std::vector<NvDsInferObjectDetectionInfo>& detectObjs,
    uint32_t batchIdx)
{
  INFER_ASSERT(inOptions);
  GstBuffer* gstBuf = nullptr;
  NvDsBatchMeta* batchMeta = nullptr;
  std::vector<NvDsFrameMeta*> frameMetaList;
  NvBufSurface* bufSurf = nullptr;
  std::vector<NvBufSurfaceParams*> surfParamsList;
  int64_t unique_id = 0;

  // get GstBuffer
  if (inOptions->hasValue(OPTION_NVDS_GST_BUFFER)) {
    INFER_ASSERT(inOptions->getObj(OPTION_NVDS_GST_BUFFER, gstBuf) == NVDSINFER_SUCCESS);
  }
  INFER_ASSERT(gstBuf);

  // get NvBufSurface
  if (inOptions->hasValue(OPTION_NVDS_BUF_SURFACE)) {
    INFER_ASSERT(inOptions->getObj(OPTION_NVDS_BUF_SURFACE, bufSurf) == NVDSINFER_SUCCESS);
  }
  INFER_ASSERT(bufSurf);

  // get NvDsBatchMeta
  if (inOptions->hasValue(OPTION_NVDS_BATCH_META)) {
    INFER_ASSERT(inOptions->getObj(OPTION_NVDS_BATCH_META, batchMeta) == NVDSINFER_SUCCESS);
  }
  INFER_ASSERT(batchMeta);

  // get all frame meta list into vector<NvDsFrameMeta*>
  if (inOptions->hasValue(OPTION_NVDS_FRAME_META_LIST)) {
    INFER_ASSERT(
        inOptions->getValueArray(OPTION_NVDS_FRAME_META_LIST, frameMetaList) ==
        NVDSINFER_SUCCESS);
  }
  INFER_ASSERT(batchIdx < frameMetaList.size());  // batchsize

  // get unique_id
  if (inOptions->hasValue(OPTION_NVDS_UNIQUE_ID)) {
    INFER_ASSERT(inOptions->getInt(OPTION_NVDS_UNIQUE_ID, unique_id) == NVDSINFER_SUCCESS);
  }

  // get all surface params list into vector<NvBufSurfaceParams*>
  if (inOptions->hasValue(OPTION_NVDS_BUF_SURFACE_PARAMS_LIST)) {
    INFER_ASSERT(
        inOptions->getValueArray(OPTION_NVDS_BUF_SURFACE_PARAMS_LIST, surfParamsList) ==
        NVDSINFER_SUCCESS);
  }
  INFER_ASSERT(batchIdx < surfParamsList.size());  // batchsize

  // attach object's boundingbox
  float max_score = -10.0;
  int obj_index = 0;
  int max_index = -1;
  // find the highest patient score object
  for (const auto& obj : detectObjs) {
    if(obj.classId == 0) { //patient class
      if(obj.detectionConfidence > max_score) {
        max_score = obj.detectionConfidence;
        max_index = obj_index;
      }
    }
    obj_index += 1;
  }
  obj_index = -1;
  for (const auto& obj : detectObjs) {

    //if (obj.detectionConfidence < threshold)
    //   continue;

    /* Check the strongest patient detection against smaller threshold */
    obj_index += 1;
    if(obj_index != max_index)
      continue;
    if(obj.detectionConfidence < 0.7*threshold)
      continue;

    //lsprintf("score %f\n",obj.detectionConfidence);

    NvDsObjectMeta* objMeta = nvds_acquire_obj_meta_from_pool(batchMeta);
    objMeta->unique_component_id = unique_id;
    objMeta->confidence = obj.detectionConfidence;
    NvDsFrameMeta* frameMeta = frameMetaList[batchIdx];
    int height = surfParamsList[batchIdx]->height;

    /* This is an untracked object. Set tracking_id to -1. */
    objMeta->object_id = UNTRACKED_OBJECT_ID;
    objMeta->class_id = obj.classId;

    NvOSD_RectParams& rect_params = objMeta->rect_params;
    NvOSD_TextParams& text_params = objMeta->text_params;

    rect_params.left = obj.left;
    rect_params.top = obj.top;
    rect_params.width = obj.width;
    rect_params.height = obj.height;

    /* Border of width 3. */
    rect_params.border_width = 3;
    rect_params.has_bg_color = 0;
    rect_params.border_color = (NvOSD_ColorParams){1, 0, 0, 1};

    /* display_text requires heap allocated memory. */
    if (_found_user_meta[frameMeta->source_id] == false){
      if ((obj.classId < mClassLabels.size())){
          text_params.display_text = g_strdup_printf("%s (%.2f)",mClassLabels[obj.classId].c_str(),
          objMeta->confidence); 
          strncpy(objMeta->obj_label, mClassLabels[obj.classId].c_str(), MAX_LABEL_SIZE - 1);
          objMeta->obj_label[MAX_LABEL_SIZE - 1] = 0;
      }
    }
    else {
      if ((obj.classId < _label_vector[frameMeta->source_id].size())){
          text_params.display_text = g_strdup_printf("%s (%.2f)",_label_vector[frameMeta->source_id][obj.classId].c_str(),
          objMeta->confidence); 
          strncpy(objMeta->obj_label, _label_vector[frameMeta->source_id][obj.classId].c_str(), MAX_LABEL_SIZE - 1);
          objMeta->obj_label[MAX_LABEL_SIZE - 1] = 0;
      }

    }


    /* Display text above the left top corner of the object. */
    text_params.x_offset = rect_params.left;
    text_params.y_offset = rect_params.top - 10;
    /* Set black background for the text. */
    text_params.set_bg_clr = 1;
    text_params.text_bg_clr = (NvOSD_ColorParams){0, 0, 0, 1};
    /* Font face, size and color. */
    text_params.font_params.font_name = (gchar*)"Serif";
    if (height < 480){
      text_params.font_params.font_size = 9;
    }
    else if ((height >=480) && (height < 720)) {
      text_params.font_params.font_size = 11;
    }
    else if ((height >=720) && (height < 1080)) {
      text_params.font_params.font_size = 14;
    }
    else if ((height >=1080) && (height < 2160)) {
      text_params.font_params.font_size = 17;
    }
    else if ((height >=2160)) {
      text_params.font_params.font_size = 20;
    }
    
    text_params.font_params.font_color = (NvOSD_ColorParams){1, 1, 1, 1};

    nvds_acquire_meta_lock(batchMeta);
    nvds_add_obj_meta_to_frame(frameMetaList[batchIdx], objMeta, NULL);
    frameMetaList[batchIdx]->bInferDone = TRUE;
    nvds_release_meta_lock(batchMeta);
  }

  return NVDSINFER_SUCCESS;
}
