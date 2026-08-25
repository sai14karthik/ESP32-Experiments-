#pragma once

// seq_tracker.hpp — per-device sequence-gap tracking. THE LAYER ABOVE.
//
// Deliberately NOT part of IUdpReceiver.

#include <cstdint>
#include <unordered_map>

namespace csi {

// Outcome of one observe() call. Splitting "lost" from "reset" lets the caller
// (and the tests) tell a genuine packet-loss gap apart from a device restart —
// otherwise a backwards jump looks identical to "no forward progress" (0).
struct SeqObservation {
    std::uint32_t lost  = 0;      // packets skipped immediately before this seq
    bool          reset = false;  // seq went backwards => treat as a device reset
};

class SeqGapTracker {
public:
    // Record `seq` for `device_id` and classify it against that device's last
    // seen sequence:
    //   * forward by N+1   -> lost = N        (N packets dropped in the hole)
    //   * exactly +1       -> lost = 0        (contiguous, in order)
    //   * first ever seen  -> lost = 0        (this seq becomes the baseline)
    //   * duplicate (==)   -> lost = 0        (no progress; not a reset)
    //   * backwards (<)    -> reset = true    (device restarted its counter)
    // Either way the device's baseline advances to `seq`, so the stream resumes
    // cleanly from wherever it now is (including straight after a reset).
    //
    // NOT thread-safe: SingleThreadUdpReceiver serializes sink calls, so calling
    // this from the sink is safe. Under a concurrent receiver, guard it (or
    // shard one tracker per device_id so each is touched by one thread).
    SeqObservation observe(std::uint8_t device_id, std::uint32_t seq) {
        auto [it, inserted] = last_.try_emplace(device_id, seq);
        if (inserted) {
            return {};  // first frame from this device: baseline only
        }

        const std::uint32_t last = it->second;
        SeqObservation obs;
        if (seq > last) {            // forward progress; count any hole
            obs.lost = seq - (last + 1);
        } else if (seq < last) {     // counter went backwards
            obs.reset = true;
        }                            // seq == last: duplicate -> neither
        it->second = seq;            // re-baseline (including after a reset)
        return obs;
    }

    void reset() { last_.clear(); }

private:
    std::unordered_map<std::uint8_t, std::uint32_t> last_;
};

}  // namespace csi
