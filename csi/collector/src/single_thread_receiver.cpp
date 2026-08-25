// single_thread_receiver.cpp — one socket, one thread, demux by device_id.

#include "csi/single_thread_receiver.hpp"

#include <arpa/inet.h>    // inet_pton, htons
#include <netinet/in.h>   // sockaddr_in
#include <sys/socket.h>   // socket, setsockopt, getsockopt, recvfrom, bind
#include <sys/time.h>     // timeval (SO_RCVTIMEO)
#include <unistd.h>       // close

#include <array>
#include <cerrno>
#include <cstring>        // std::strerror
#include <iostream>
#include <span>
#include <stdexcept>
#include <system_error>

namespace csi {

SingleThreadUdpReceiver::SingleThreadUdpReceiver(Config cfg, FrameSink sink)
    : cfg_(std::move(cfg)), sink_(std::move(sink)) {}

SingleThreadUdpReceiver::~SingleThreadUdpReceiver() {
    stop();  // RAII: never leak the worker thread or the fd.
}

bool SingleThreadUdpReceiver::running() const noexcept {
    return running_.load(std::memory_order_acquire);
}

ReceiverStats SingleThreadUdpReceiver::stats() const noexcept {
    return ReceiverStats{
        frames_.load(std::memory_order_relaxed),
        bytes_.load(std::memory_order_relaxed),
        malformed_.load(std::memory_order_relaxed),
    };
}

void SingleThreadUdpReceiver::start() {
    if (running_.load(std::memory_order_acquire)) {
        throw std::logic_error("SingleThreadUdpReceiver::start: already running");
    }

    fd_ = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (fd_ < 0) {
        throw std::system_error(errno, std::generic_category(), "socket");
    }

    // Anything past this point that fails must close fd_ before throwing.
    try {
        // Allow quick rebind after restart (avoids "Address already in use").
        int yes = 1;
        ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);

        // --- Enlarge SO_RCVBUF, then read back the ACTUAL value and log it ----
        // The kernel may clamp the request to kern.ipc.maxsockbuf, so the value
        // you set is not necessarily the value you get — always read it back.
        int want = cfg_.desired_rcvbuf_bytes;
        if (::setsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &want, sizeof want) < 0) {
            std::clog << "[csi] setsockopt(SO_RCVBUF) failed: "
                      << std::strerror(errno) << " (continuing with default)\n";
        }
        int actual = 0;
        socklen_t alen = sizeof actual;
        if (::getsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &actual, &alen) == 0) {
            std::clog << "[csi] SO_RCVBUF requested=" << want
                      << " actual=" << actual
                      << " bytes (kernel may clamp to kern.ipc.maxsockbuf)\n";
        } else {
            std::clog << "[csi] getsockopt(SO_RCVBUF) failed: "
                      << std::strerror(errno) << "\n";
        }

        // recv() timeout so the blocking recvfrom periodically returns and the
        // worker can observe the stop flag. (See shutdown note at bottom.)
        timeval tv{};
        tv.tv_sec  = static_cast<time_t>(cfg_.poll_interval.count() / 1000);
        tv.tv_usec = static_cast<suseconds_t>((cfg_.poll_interval.count() % 1000) * 1000);
        ::setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

        // Bind.
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port   = htons(cfg_.port);
        if (::inet_pton(AF_INET, cfg_.bind_address.c_str(), &addr.sin_addr) != 1) {
            throw std::invalid_argument("SingleThreadUdpReceiver::start: bad bind_address '"
                                        + cfg_.bind_address + "'");
        }
        if (::bind(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof addr) < 0) {
            throw std::system_error(errno, std::generic_category(), "bind");
        }
    } catch (...) {
        ::close(fd_);
        fd_ = -1;
        throw;
    }

    // Publish "running" before the worker starts so run()'s loop sees it true.
    running_.store(true, std::memory_order_release);
    worker_ = std::thread([this] { run(); });
}

void SingleThreadUdpReceiver::stop() noexcept {
    // exchange(false) returns the prior value: only the transition true->false
    // does the teardown, so concurrent / repeated stop() calls are harmless.
    if (!running_.exchange(false, std::memory_order_acq_rel)) {
        return;
    }
    if (worker_.joinable()) {
        worker_.join();  // worker exits within one poll_interval
    }
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

void SingleThreadUdpReceiver::run() {
    // +1 so an oversize datagram fills the buffer and recvfrom returns kFrameLen+1,
    // letting parse_frame reject it as malformed (rather than silent truncation).
    std::array<std::byte, kFrameLen + 1> buf;

    while (running_.load(std::memory_order_acquire)) {
        sockaddr_in src{};
        socklen_t   srclen = sizeof src;
        ssize_t n = ::recvfrom(fd_, buf.data(), buf.size(), 0,
                               reinterpret_cast<sockaddr*>(&src), &srclen);

        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;  // recv timeout — loop back and re-check running_
            }
            if (errno == EINTR) {
                continue;  // interrupted by a signal — retry
            }
            if (!running_.load(std::memory_order_acquire)) {
                break;     // fd closed underneath us during stop()
            }
            std::clog << "[csi] recvfrom error: " << std::strerror(errno) << "\n";
            continue;
        }

        std::printf("Received udp message");

        // Stamp arrival as early as possible, both clocks at the same instant:
        // steady for timing math, wall for logging/correlation.
        const auto t_steady = std::chrono::steady_clock::now();
        const auto t_wall   = std::chrono::system_clock::now();

        bytes_.fetch_add(static_cast<std::uint64_t>(n), std::memory_order_relaxed);

        auto frame = parse_frame(
            std::span<const std::byte>(buf.data(), static_cast<std::size_t>(n)),
            t_steady, t_wall);
        if (!frame) {
            malformed_.fetch_add(1, std::memory_order_relaxed);  // drop, don't crash
            continue;
        }

        frames_.fetch_add(1, std::memory_order_relaxed);
        if (sink_) {
            sink_(std::move(*frame));  // delivered on this (the worker) thread
        }
    }
}

}  // namespace csi
