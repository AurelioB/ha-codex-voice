#include "codex_aec3_capture.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <limits>
#include <memory>
#include <new>
#include <string>
#include <utility>

#include "capture_engine.h"

struct codex_aec3_handle {
    explicit codex_aec3_handle(codex::aec3::CaptureConfig config)
        : engine(std::move(config)) {}

    codex::aec3::CaptureEngine engine;
};

namespace {

bool ConfigHeaderIsValid(const codex_aec3_config* config) {
    return config != nullptr &&
        config->struct_size >= sizeof(codex_aec3_config) &&
        config->abi_version == CODEX_AEC3_ABI_VERSION;
}

}  // namespace

extern "C" {

uint32_t codex_aec3_abi_version(void) {
    return CODEX_AEC3_ABI_VERSION;
}

void codex_aec3_default_config(codex_aec3_config* config) {
    if (config == nullptr) {
        return;
    }
    *config = {};
    config->struct_size = sizeof(*config);
    config->abi_version = CODEX_AEC3_ABI_VERSION;
    config->alsa_device = "hw:0,4";
    config->sample_rate = codex::aec3::kAec3SampleRate;
    config->channels = 4;
    config->mic_channel = 0;
    config->reference_channel_a = 2;
    config->reference_channel_b = 3;
    config->period_frames = codex::aec3::kAec3FrameSamples;
    config->ring_frames = 4'096;
    config->startup_timeout_ms = 1'000;
}

codex_aec3_handle* codex_aec3_create(const codex_aec3_config* config) {
    if (!ConfigHeaderIsValid(config) || config->alsa_device == nullptr) {
        return nullptr;
    }
    try {
        codex::aec3::CaptureConfig native;
        native.alsa_device = config->alsa_device;
        native.sample_rate = config->sample_rate;
        native.channels = config->channels;
        native.mic_channel = config->mic_channel;
        native.reference_channel_a = config->reference_channel_a;
        native.reference_channel_b = config->reference_channel_b;
        native.period_frames = config->period_frames;
        native.ring_frames = config->ring_frames;
        native.startup_timeout =
            std::chrono::milliseconds(config->startup_timeout_ms);
        return new (std::nothrow) codex_aec3_handle(std::move(native));
    } catch (...) {
        return nullptr;
    }
}

int32_t codex_aec3_start(codex_aec3_handle* handle) {
    if (handle == nullptr) {
        return CODEX_AEC3_INVALID_ARGUMENT;
    }
    try {
        return handle->engine.Start();
    } catch (...) {
        handle->engine.Stop();
        return CODEX_AEC3_INTERNAL_ERROR;
    }
}

int32_t codex_aec3_read(
    codex_aec3_handle* handle,
    int16_t* output,
    uint32_t frames,
    uint32_t timeout_ms) {
    if (handle == nullptr) {
        return CODEX_AEC3_INVALID_ARGUMENT;
    }
    return handle->engine.Read(
        output, frames, std::chrono::milliseconds(timeout_ms));
}

int32_t codex_aec3_request_reset(codex_aec3_handle* handle) {
    return handle == nullptr
        ? CODEX_AEC3_INVALID_ARGUMENT
        : handle->engine.RequestReset();
}

int32_t codex_aec3_get_stats(
    codex_aec3_handle* handle,
    codex_aec3_stats* stats) {
    if (handle == nullptr || stats == nullptr ||
        stats->struct_size < sizeof(*stats) ||
        stats->abi_version != CODEX_AEC3_ABI_VERSION) {
        return CODEX_AEC3_INVALID_ARGUMENT;
    }
    const auto native = handle->engine.GetStats();
    stats->captured_frames = native.captured_frames;
    stats->delivered_frames = native.delivered_frames;
    stats->dropped_frames = native.dropped_frames;
    stats->recoveries = native.recoveries;
    stats->short_reads = native.short_reads;
    stats->processing_failures = native.processing_failures;
    stats->resets = native.resets;
    return CODEX_AEC3_OK;
}

size_t codex_aec3_copy_last_error(
    codex_aec3_handle* handle,
    char* output,
    size_t output_size) {
    const std::string error = handle == nullptr
        ? "invalid AEC3 capture handle"
        : handle->engine.LastError();
    const std::size_t required = error.size() + 1;
    if (output != nullptr && output_size != 0) {
        const std::size_t copy_size = std::min(error.size(), output_size - 1);
        std::memcpy(output, error.data(), copy_size);
        output[copy_size] = '\0';
    }
    return required;
}

void codex_aec3_stop(codex_aec3_handle* handle) {
    if (handle != nullptr) {
        handle->engine.Stop();
    }
}

void codex_aec3_destroy(codex_aec3_handle* handle) {
    delete handle;
}

}  // extern "C"
