ARG BASE_IMAGE=rpm-ds:1.0-blackwell-dev
FROM ${BASE_IMAGE}

RUN /opt/nvidia/deepstream/deepstream/user_additional_install.sh

COPY ./ /rpm
WORKDIR /rpm

RUN bash ./scripts/cpscript.sh && \
    make -C ./dependencies/nvdsinferserver_custom_impl_gdino/ install && \
    make -C ./dependencies/nvdsinferserver_custom_impl_hpe_ar/ install && \
    make -C ./deepstream_mycare_app CUDA_VER=12.6 install

ENTRYPOINT ["/bin/bash", "-lc", "source /rpm/scripts/install_trt_blackwell.sh && exec /rpm/scripts/start_project.sh"]


