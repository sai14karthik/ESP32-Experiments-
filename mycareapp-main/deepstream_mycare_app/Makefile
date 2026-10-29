CUDA_VER?=
ifeq ($(CUDA_VER),)
  $(error "CUDA_VER is not set")
endif

SDK_ROOT?= /opt/nvidia/deepstream/deepstream-8.0/sources/apps
APP := deepstream_mycare_app

TARGET_DEVICE = $(shell gcc -dumpmachine | cut -f1 -d -)

DEEPSTREAM_HOME:= /opt/nvidia/deepstream/deepstream-8.0
LIB_INSTALL_DIR?=$(DEEPSTREAM_HOME)/lib/
APP_INSTALL_DIR?=$(DEEPSTREAM_HOME)/bin/

ifeq ($(TARGET_DEVICE),aarch64)
  CFLAGS:= -DPLATFORM_TEGRA
endif

SRCS:= deepstream_mycare_app_main.c deepstream_app.c
SRCS+= $(SDK_ROOT)/sample_apps/deepstream-test5/deepstream_utc.c
SRCS+= $(SDK_ROOT)/sample_apps/deepstream-app/deepstream_app_config_parser.c
SRCS+= $(SDK_ROOT)/sample_apps/deepstream-app/deepstream_app_config_parser_yaml.cpp
SRCS+= $(wildcard $(SDK_ROOT)/apps-common/src/*.c)
SRCS+= $(wildcard $(SDK_ROOT)/apps-common/src/deepstream-yaml/*.cpp)

INCS:= $(wildcard *.h)

PKGS:= gstreamer-1.0 gstreamer-video-1.0 x11 json-glib-1.0

OBJS:= $(SRCS:.c=.o)
OBJS:= $(OBJS:.cpp=.o)

CFLAGS+= -I$(SDK_ROOT)/apps-common/includes \
		 -I$(SDK_ROOT)/../includes \
		 -I$(SDK_ROOT)/sample_apps/deepstream-app/ -DDS_VERSION_MINOR=0 -DDS_VERSION_MAJOR=5 \
		 -I$(SDK_ROOT)/sample_apps/deepstream-test5 \
		 -I /usr/local/cuda-$(CUDA_VER)/include

LIBS:= -L/usr/local/cuda-$(CUDA_VER)/lib64/ -lcudart

LIBS+= -L$(LIB_INSTALL_DIR) -lnvdsgst_meta -lnvds_meta -lnvdsgst_helper -lnvdsgst_customhelper -lnvdsgst_smartrecord -lnvds_utils -lnvds_msgbroker -lm \
       -lyaml-cpp -lcuda -lgstrtspserver-1.0 -ldl -Wl,-rpath,$(LIB_INSTALL_DIR)

CFLAGS+= $(shell pkg-config --cflags $(PKGS))

LIBS+= $(shell pkg-config --libs $(PKGS))

all: $(APP)

%.o: %.c $(INCS) Makefile
	$(CC) -c -o $@ $(CFLAGS) $<

%.o: %.cpp $(INCS) Makefile
	$(CXX) -c -o $@ $(CFLAGS) $<

$(APP): $(OBJS) Makefile
	$(CXX) -o $(APP) $(OBJS) $(LIBS)

install: $(APP)
	cp -v $(APP) $(APP_INSTALL_DIR)

clean:
	find . -maxdepth 1 -name '*.o' -type f -delete
	find . -maxdepth 1 -name '$(APP)' -type f -delete
