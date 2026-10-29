################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
################################################################################

CC:= g++

INFERSERVER_PROTOS := $(wildcard /opt/nvidia/deepstream/deepstream/sources/includes/*.proto)
ALL_PROTOS := $(INFERSERVER_PROTOS)
ALL_PROTOS_PBCC := $(notdir $(ALL_PROTOS:.proto=.pb.cc))
ALL_PROTOS_PBH := $(notdir $(ALL_PROTOS:.proto=.pb.h))
PROTOBUF_BIN_DIR ?= /opt/proto/bin
TRITON_GRPC_CLIENT ?= /opt/tritonclient


CFLAGS:= -Wall -std=c++17 -shared -fPIC -Wno-error=deprecated-declarations
CFLAGS+= -I/opt/nvidia/deepstream/deepstream/sources/includes \
         -I/opt/nvidia/deepstream/deepstream/sources/includes/nvdsinferserver \
	 -I/opt/tritonclient/include/


LIBS:= \
       -L/opt/nvidia/deepstream/deepstream/lib/ -lnvds_meta   -L/opt/tritonclient/lib/ -l:libprotobuf.a 
LFLAGS:= -Wl,--start-group $(LIBS) -Wl,--end-group


SRCFILES:= nvdsinferserver_custom_process_hpe_ar.cpp

TARGET_LIB:= libnvdstriton_custom_impl_hpe_ar.so

PKGS:= glib-2.0
CFLAGS+=$(shell pkg-config --cflags $(PKGS))
LIBS+=$(shell pkg-config --libs $(PKGS))

.PRECIOUS:  $(ALL_PROTOS_PBCC)
.PRECIOUS:  $(ALL_PROTOS_PBH)

all: $(TARGET_LIB)


%.pb.h %.pb.cc: /opt/nvidia/deepstream/deepstream/sources/includes/%.proto $(INFERSERVER_PROTOS) Makefile
	@mkdir -p $(dir $@)
	$(PROTOBUF_BIN_DIR)/protoc --proto_path=/opt/nvidia/deepstream/deepstream/sources/includes --cpp_out=. $<

$(TARGET_LIB) : $(SRCFILES) $(ALL_PROTOS_PBCC) $(ALL_PROTOS_PBH)
	$(CC) -o $@ $^ $(CFLAGS) $(LFLAGS)

clean:
	rm -rf $(TARGET_LIB) *.o *.h *.cc

install: $(TARGET_LIB)
	cp $(TARGET_LIB) /opt/nvidia/deepstream/deepstream/lib/

