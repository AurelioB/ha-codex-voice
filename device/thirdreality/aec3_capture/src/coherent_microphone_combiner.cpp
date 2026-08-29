#include "coherent_microphone_combiner.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace codex::aec3 {

namespace {

constexpr double kMinimumRms = 16.0;
constexpr double kMinimumAbsoluteCorrelation = 0.60;
constexpr double kMinimumEnergyRatio = 0.50;
constexpr double kMaximumEnergyRatio = 1.0 / kMinimumEnergyRatio;
constexpr float kBlendStep = 0.25F;
constexpr int kMaximumLagSamples = 4;

struct CorrelationResult {
    double correlation = 0.0;
    double primary_rms = 0.0;
    double secondary_rms = 0.0;
};

CorrelationResult CorrelateAtLag(
    const std::int16_t* primary,
    const std::int16_t* secondary,
    std::size_t frames,
    int lag) {
    const std::size_t primary_start = lag < 0
        ? static_cast<std::size_t>(-lag)
        : 0;
    const std::size_t secondary_start = lag > 0
        ? static_cast<std::size_t>(lag)
        : 0;
    const std::size_t count = frames - std::max(primary_start, secondary_start);
    double primary_sum = 0.0;
    double secondary_sum = 0.0;
    double primary_square_sum = 0.0;
    double secondary_square_sum = 0.0;
    double product_sum = 0.0;
    for (std::size_t offset = 0; offset < count; ++offset) {
        const double a = primary[primary_start + offset];
        const double b = secondary[secondary_start + offset];
        primary_sum += a;
        secondary_sum += b;
        primary_square_sum += a * a;
        secondary_square_sum += b * b;
        product_sum += a * b;
    }
    const double sample_count = static_cast<double>(count);
    const double primary_energy = std::max(
        0.0, primary_square_sum - primary_sum * primary_sum / sample_count);
    const double secondary_energy = std::max(
        0.0, secondary_square_sum - secondary_sum * secondary_sum / sample_count);
    const double covariance =
        product_sum - primary_sum * secondary_sum / sample_count;
    const double denominator = std::sqrt(primary_energy * secondary_energy);
    CorrelationResult result;
    result.correlation = denominator > 0.0 ? covariance / denominator : 0.0;
    result.primary_rms = std::sqrt(primary_energy / sample_count);
    result.secondary_rms = std::sqrt(secondary_energy / sample_count);
    return result;
}

std::int16_t ClampPcm16(double value) {
    const double rounded = std::round(value);
    return static_cast<std::int16_t>(std::clamp(
        rounded,
        static_cast<double>(std::numeric_limits<std::int16_t>::min()),
        static_cast<double>(std::numeric_limits<std::int16_t>::max())));
}

}  // namespace

void CoherentMicrophoneCombiner::Reset() {
    blend_ = 0.0F;
    polarity_ = 1.0F;
    lag_samples_ = 0;
}

CoherentMicrophoneCombiner::Result CoherentMicrophoneCombiner::Process(
    const std::int16_t* primary,
    const std::int16_t* secondary,
    std::int16_t* output,
    std::size_t frames) {
    if (primary == nullptr || secondary == nullptr || output == nullptr ||
        frames == 0) {
        return {};
    }
    if (frames <= static_cast<std::size_t>(kMaximumLagSamples * 2)) {
        std::copy_n(primary, frames, output);
        Reset();
        return {};
    }

    CorrelationResult best;
    int best_lag = 0;
    for (int lag = -kMaximumLagSamples; lag <= kMaximumLagSamples; ++lag) {
        const auto candidate = CorrelateAtLag(primary, secondary, frames, lag);
        if (std::abs(candidate.correlation) > std::abs(best.correlation)) {
            best = candidate;
            best_lag = lag;
        }
    }
    const double energy_ratio = best.primary_rms > 0.0
        ? best.secondary_rms / best.primary_rms
        : std::numeric_limits<double>::infinity();
    const bool coherent = best.primary_rms >= kMinimumRms &&
        best.secondary_rms >= kMinimumRms &&
        std::abs(best.correlation) >= kMinimumAbsoluteCorrelation &&
        energy_ratio >= kMinimumEnergyRatio &&
        energy_ratio <= kMaximumEnergyRatio;
    if (coherent) {
        polarity_ = best.correlation < 0.0 ? -1.0F : 1.0F;
        lag_samples_ = best_lag;
    }

    blend_ = std::clamp(
        blend_ + (coherent ? kBlendStep : -kBlendStep), 0.0F, 1.0F);
    const double secondary_weight = static_cast<double>(blend_) * 0.5;
    const double primary_weight = 1.0 - secondary_weight;
    const double polarity = static_cast<double>(polarity_);
    for (std::size_t frame = 0; frame < frames; ++frame) {
        const int secondary_index = static_cast<int>(frame) + lag_samples_;
        const double aligned_secondary = secondary_index >= 0 &&
                secondary_index < static_cast<int>(frames)
            ? static_cast<double>(secondary[secondary_index])
            : static_cast<double>(primary[frame]) * polarity;
        output[frame] = ClampPcm16(
            primary_weight * static_cast<double>(primary[frame]) +
            secondary_weight * polarity * aligned_secondary);
    }
    Result result;
    result.coherent = coherent;
    result.blend = blend_;
    return result;
}

}  // namespace codex::aec3
