/*
 * Stable C boundary for the ThirdReality hardware-loopback AEC3 capture path.
 *
 * The ABI deliberately exposes PCM16 frames and plain-old-data structures so
 * the Python 3.11 voice process does not depend on a C++ extension ABI.
 */

#ifndef CODEX_AEC3_CAPTURE_H
#define CODEX_AEC3_CAPTURE_H

#include <stddef.h>
#include <stdint.h>

#if defined(__GNUC__)
#define CODEX_AEC3_EXPORT __attribute__((visibility("default")))
#else
#define CODEX_AEC3_EXPORT
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define CODEX_AEC3_ABI_VERSION 2u

typedef struct codex_aec3_handle codex_aec3_handle;

enum codex_aec3_status {
    CODEX_AEC3_OK = 0,
    CODEX_AEC3_INVALID_ARGUMENT = -1,
    CODEX_AEC3_INVALID_STATE = -2,
    CODEX_AEC3_ALSA_ERROR = -3,
    CODEX_AEC3_PROCESSING_ERROR = -4,
    CODEX_AEC3_TIMEOUT = -5,
    CODEX_AEC3_STOPPED = -6,
    CODEX_AEC3_RING_OVERFLOW = -7,
    CODEX_AEC3_INTERNAL_ERROR = -8,
};

typedef struct codex_aec3_config {
    uint32_t struct_size;
    uint32_t abi_version;
    const char* alsa_device;
    uint32_t sample_rate;
    uint32_t channels;
    uint32_t mic_channel;
    int32_t secondary_mic_channel;
    int32_t reference_channel_a;
    int32_t reference_channel_b;
    uint32_t period_frames;
    uint32_t ring_frames;
    uint32_t startup_timeout_ms;
} codex_aec3_config;

typedef struct codex_aec3_stats {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t captured_frames;
    uint64_t delivered_frames;
    uint64_t dropped_frames;
    uint64_t recoveries;
    uint64_t short_reads;
    uint64_t processing_failures;
    uint64_t resets;
    uint64_t coherent_mic_frames;
    uint64_t primary_only_mic_frames;
} codex_aec3_stats;

CODEX_AEC3_EXPORT uint32_t codex_aec3_abi_version(void);

CODEX_AEC3_EXPORT void codex_aec3_default_config(codex_aec3_config* config);

CODEX_AEC3_EXPORT codex_aec3_handle* codex_aec3_create(
    const codex_aec3_config* config);

CODEX_AEC3_EXPORT int32_t codex_aec3_start(codex_aec3_handle* handle);

/*
 * Return a positive frame count, or a negative codex_aec3_status. A successful
 * read may be shorter than requested; callers that require an exact block must
 * continue reading. Samples are mono, signed 16-bit, native little-endian.
 */
CODEX_AEC3_EXPORT int32_t codex_aec3_read(
    codex_aec3_handle* handle,
    int16_t* output,
    uint32_t frames,
    uint32_t timeout_ms);

CODEX_AEC3_EXPORT int32_t codex_aec3_request_reset(
    codex_aec3_handle* handle);

CODEX_AEC3_EXPORT int32_t codex_aec3_get_stats(
    codex_aec3_handle* handle,
    codex_aec3_stats* stats);

/* Return the required buffer size, including the trailing NUL. */
CODEX_AEC3_EXPORT size_t codex_aec3_copy_last_error(
    codex_aec3_handle* handle,
    char* output,
    size_t output_size);

CODEX_AEC3_EXPORT void codex_aec3_stop(codex_aec3_handle* handle);

CODEX_AEC3_EXPORT void codex_aec3_destroy(codex_aec3_handle* handle);

#ifdef __cplusplus
}
#endif

#endif
