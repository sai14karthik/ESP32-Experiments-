#pragma once

// single_thread_receiver.hpp — the simplest concrete IUdpReceiver.
//
// One socket, one worker thread. All devices' datagrams arrive interleaved on
// the same socket; we read device_id from each header and hand up a Frame. The
// per-device "demux" is therefore just reading a field — the layer above keys
// on Frame::device_id. A future per-device-socket or kqueue receiver produces
// identical Frames, so swapping it in changes nothing above this class.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <string>
#include <thread>

#include "csi/udp_receiver.hpp"

namespace csi {

class SingleThreadUdpReceiver final : public IUdpReceiver {
public:
    struct Config {
        std::uint16_t port = 0;                 // UDP port to bind
        std::string   bind_address = "0.0.0.0"; // INADDR_ANY by default

        // Requested kernel receive buffer.
        int desired_rcvbuf_bytes = 4 * 1024 * 1024;  // 4 MiB

        // recv() timeout. Bounds how long stop() waits for the worker to notice
        // the shutdown flag
        std::chrono::milliseconds poll_interval{200};
    };

    // Construct with a config and a sink. The sink is called on the worker
    // thread, so it must be thread-safe. The sink is expected to consume the
    // Frame (move it) and return quickly; the receiver does not queue frames.
    SingleThreadUdpReceiver(Config cfg, FrameSink sink);
    ~SingleThreadUdpReceiver() override;  // calls stop()

    void start() override;            // throws std::system_error / std::*_error
    void stop() noexcept override;
    bool running() const noexcept override;
    ReceiverStats stats() const noexcept override;

private:
    void run();  // the receive loop; runs on worker_

    Config    cfg_;
    FrameSink sink_;
    int       fd_ = -1;
    std::thread worker_;
    std::atomic<bool> running_{false};

    // Counters are written only by the worker thread and read by stats(); atomic
    // so a reader on another thread sees consistent values.
    std::atomic<std::uint64_t> frames_{0};
    std::atomic<std::uint64_t> bytes_{0};
    std::atomic<std::uint64_t> malformed_{0};
};

}  // namespace csi
