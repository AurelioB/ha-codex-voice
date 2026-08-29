#pragma once

#include <cstddef>
#include <cstdint>

namespace codex::aec3 {

// Conservatively combines the two physical microphones only when a 10 ms
// frame contains strongly correlated, similarly sized signals. Otherwise the
// primary microphone is copied byte-for-byte. A short ramp prevents clicks as
// the coherent contribution enters or leaves the mix.
class CoherentMicrophoneCombiner {
   public:
    struct Result {
        bool coherent = false;
        float blend = 0.0F;
    };

    void Reset();
    Result Process(
        const std::int16_t* primary,
        const std::int16_t* secondary,
        std::int16_t* output,
        std::size_t frames);

   private:
    float blend_ = 0.0F;
    float polarity_ = 1.0F;
    int lag_samples_ = 0;
};

}  // namespace codex::aec3
