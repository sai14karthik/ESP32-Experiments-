#pragma once

// udp_receiver.hpp — the implementation-agnostic receiver abstraction.
//
// IUdpReceiver hides HOW datagrams are received (how many sockets, how many
// threads, blocking recv vs. kqueue). Calling code depends only on this header
// and on frame.hpp. Swapping a concrete implementation never changes a caller.

#include <cstdint>
#include <functional>

#include "csi/frame.hpp"

namespace csi {

// ---------------------------------------------------------------------------
// FrameSink — the chosen delivery mechanism.
//
// One call per validated datagram. The Frame is moved in, so the sink owns it
// for the duration of the call (and may move it onward).
//
// THREADING CONTRACT (the guarantee every IUdpReceiver makes):
//   * The sink is invoked on a RECEIVER-OWNED thread, never the caller's.
//   * It may be invoked CONCURRENTLY from one or more such threads. Write the
//     sink to be thread-safe unless the concrete implementation documents a
//     stronger guarantee. (SingleThreadUdpReceiver documents serialized,
//     single-thread delivery — so a sink used only with it needs no lock.)
//   * Keep it SHORT. It runs inside the receive loop; if it blocks, the loop
//     stalls and the kernel socket buffer absorbs the slack until it overflows
//     and the OS drops packets. That is the back-pressure model: kernel buffer
//     + sink speed. If you need bounded-queue back-pressure instead, have the
//     sink push into your own bounded queue with your own drop/block policy.
//   * Do NOT call the receiver's stop() from inside the sink (it would join the
//     thread the sink runs on).
using FrameSink = std::function<void(Frame&&)>;

// Cross-cutting counters every implementation maintains. Snapshot value type.
struct ReceiverStats {
    std::uint64_t frames_delivered = 0;  // valid frames passed to the sink
    std::uint64_t bytes_received   = 0;  // total payload bytes off the socket(s)
    std::uint64_t malformed_dropped = 0; // datagrams rejected by parse_frame
};

// ---------------------------------------------------------------------------
// IUdpReceiver — lifecycle + observability, nothing implementation-specific.
//
// Construction (port, buffer sizes, the sink) is the concrete type's business,
// so the abstract base never dictates it. That keeps the interface a pure,
// stable seam: a single-socket demux, a per-device-socket fan-in, or a kqueue
// event loop all satisfy exactly this.
// ---------------------------------------------------------------------------
class IUdpReceiver {
public:
    virtual ~IUdpReceiver() = default;

    // Begin receiving and delivering frames. Throws on socket/bind failure.
    // After a successful return, frames may start arriving at the sink.
    virtual void start() = 0;

    // Graceful shutdown. After stop() returns: no sink call is in flight and
    // none will start. Idempotent; safe to call from a thread other than the
    // receiver's worker (but not from inside the sink itself).
    virtual void stop() noexcept = 0;

    virtual bool running() const noexcept = 0;
    virtual ReceiverStats stats() const noexcept = 0;

protected:
    IUdpReceiver() = default;

    // A receiver owns threads and sockets — identity matters, so it is neither
    // copyable nor movable. Hand it around by reference or unique_ptr.
    IUdpReceiver(const IUdpReceiver&)            = delete;
    IUdpReceiver& operator=(const IUdpReceiver&) = delete;
    IUdpReceiver(IUdpReceiver&&)                 = delete;
    IUdpReceiver& operator=(IUdpReceiver&&)      = delete;
};

}  // namespace csi
