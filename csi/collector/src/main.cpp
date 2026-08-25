// main.cpp — wire a concrete receiver to a consumer and run until Ctrl-C.
//
// Note how little this knows: it talks to SingleThreadUdpReceiver only to
// construct/start/stop it. The sink and the SeqGapTracker depend solely on
// Frame. Swap in a per-device-socket or kqueue receiver and this file's logic
// is unchanged (only the concrete type named at construction differs).

#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <thread>

#include "csi/banner.hpp"
#include "csi/seq_tracker.hpp"
#include "csi/single_thread_receiver.hpp"

namespace {
// async-signal-safe handoff from the SIGINT handler to main's loop.
std::atomic<bool> g_stop{false};
void on_signal(int /*sig*/) { g_stop.store(true); }
}  // namespace

int main() {
    static_assert(__cplusplus >= 202002L, "Expected C++20 (-std=c++20) or newer");
    std::cout << csi::kBanner << '\n';

    // The "layer above" the receiver — owns per-device sequence state.
    csi::SeqGapTracker tracker;

    csi::SingleThreadUdpReceiver::Config cfg;
    cfg.port = 5566;

    // Sink runs on the receiver's worker thread. SingleThreadUdpReceiver
    // serializes calls on ONE thread, so `tracker` needs no lock here. Keep the
    // body short — it runs inside the receive loop.
    csi::SingleThreadUdpReceiver receiver(cfg, [&tracker](csi::Frame&& f) {
        const csi::SeqObservation obs = tracker.observe(f.device_id, f.seq);
        if (obs.reset) {
            std::cout << "[reset] device=" << static_cast<int>(f.device_id)
                      << " sequence jumped back to seq=" << f.seq << '\n';
        } else if (obs.lost > 0) {
            std::cout << "[gap] device=" << static_cast<int>(f.device_id)
                      << " missed " << obs.lost << " before seq=" << f.seq << '\n';
        }
        // Real consumer would hand f.payload (kPayloadLen opaque bytes) to CSI
        // processing here — or push the Frame into your own bounded queue if you
        // want queue-style back-pressure instead of running work inline.
    });

    std::signal(SIGINT, on_signal);

    try {
        receiver.start();  // throws on socket/bind failure
    } catch (const std::exception& e) {
        std::cerr << "failed to start receiver: " << e.what() << '\n';
        return 1;
    }
    std::cout << "listening on udp/" << cfg.port << " — Ctrl-C to stop\n";

    while (!g_stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    receiver.stop();  // graceful: no sink call in flight after this returns
    const auto s = receiver.stats();
    std::cout << "\nstopped. frames=" << s.frames_delivered
              << " bytes="     << s.bytes_received
              << " malformed=" << s.malformed_dropped << '\n';
    return 0;
}
