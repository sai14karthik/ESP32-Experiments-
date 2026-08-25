// seq_tracker_test.cpp — unit tests for per-device sequence-gap detection.
//
// Covers the header-only SeqGapTracker in csi/seq_tracker.hpp: a clean stream
// reports no loss, a hole reports the number of missing packets, a backwards
// sequence is flagged as a device reset, and devices are tracked independently.

#include <catch2/catch_test_macros.hpp>

#include <cstdint>

#include "csi/seq_tracker.hpp"

using namespace csi;

TEST_CASE("a clean in-order stream reports zero loss", "[seq][gap]") {
    SeqGapTracker t;
    CHECK(t.observe(1, 0).lost == 0);  // first seen: establishes the baseline
    for (std::uint32_t s = 1; s <= 5; ++s) {
        const auto obs = t.observe(1, s);
        CHECK(obs.lost == 0);
        CHECK_FALSE(obs.reset);
    }
}

TEST_CASE("a hole reports the number of lost packets", "[seq][gap]") {
    SeqGapTracker t;
    REQUIRE(t.observe(1, 100).lost == 0);  // baseline at 100

    SECTION("one missing packet") {
        const auto obs = t.observe(1, 102);  // 101 is missing
        CHECK(obs.lost == 1);
        CHECK_FALSE(obs.reset);
    }
    SECTION("several missing packets") {
        const auto obs = t.observe(1, 105);  // 101..104 missing
        CHECK(obs.lost == 4);
        CHECK_FALSE(obs.reset);
    }
    SECTION("no hole on the very next sequence") {
        const auto obs = t.observe(1, 101);
        CHECK(obs.lost == 0);
        CHECK_FALSE(obs.reset);
    }
    SECTION("a duplicate is neither loss nor reset") {
        const auto obs = t.observe(1, 100);
        CHECK(obs.lost == 0);
        CHECK_FALSE(obs.reset);
    }
}

TEST_CASE("a backwards sequence registers a device reset", "[seq][reset]") {
    SeqGapTracker t;
    REQUIRE(t.observe(2, 1000).lost == 0);  // baseline high

    SECTION("the backwards jump is flagged as a reset, not as loss") {
        const auto obs = t.observe(2, 5);
        CHECK(obs.reset);
        CHECK(obs.lost == 0);
    }
    SECTION("the stream resumes cleanly from the new (lower) baseline") {
        REQUIRE(t.observe(2, 5).reset);     // reset down to 5
        const auto obs = t.observe(2, 6);    // in order from the new baseline
        CHECK(obs.lost == 0);
        CHECK_FALSE(obs.reset);
    }
}

TEST_CASE("devices are tracked independently", "[seq][multidev]") {
    SeqGapTracker t;
    CHECK(t.observe(1, 0).lost == 0);
    CHECK(t.observe(2, 0).lost == 0);
    CHECK(t.observe(1, 1).lost == 0);
    CHECK(t.observe(2, 10).lost == 9);  // device 2 dropped seq 1..9
    CHECK(t.observe(1, 2).lost == 0);   // device 1 is still contiguous
    CHECK(t.observe(2, 11).lost == 0);  // device 2 resumes from 10
}

TEST_CASE("the first observation of a device is a baseline, never a gap", "[seq]") {
    SeqGapTracker t;
    const auto obs = t.observe(9, 123456);  // large first seq, but still a baseline
    CHECK(obs.lost == 0);
    CHECK_FALSE(obs.reset);
}

TEST_CASE("reset() forgets all per-device state", "[seq]") {
    SeqGapTracker t;
    REQUIRE(t.observe(1, 5).lost == 0);
    t.reset();
    const auto obs = t.observe(1, 100);  // fresh baseline, NOT a gap of 94
    CHECK(obs.lost == 0);
    CHECK_FALSE(obs.reset);
}
