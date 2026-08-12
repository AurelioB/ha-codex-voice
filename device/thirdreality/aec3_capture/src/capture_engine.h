#pragma once

#include <alsa/asoundlib.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace webrtc {
class AudioProcessing;
class StreamConfig;
}  // namespace webrtc

namespace codex::aec3 {

constexpr unsigned kAec3SampleRate = 16'000;
constexpr std::size_t kAec3FrameSamples = 160;

struct CaptureConfig {
    std::string alsa_device = "hw:0,4";
    unsigned sample_rate = kAec3SampleRate;
    unsigned channels = 4;
    unsigned mic_channel = 0;
    int reference_channel_a = 2;
    int reference_channel_b = 3;
    std::size_t period_frames = kAec3FrameSamples;
    std::size_t ring_frames = 4'096;
    std::chrono::milliseconds startup_timeout{1'000};
};

struct CaptureStats {
    std::uint64_t captured_frames = 0;
    std::uint64_t delivered_frames = 0;
    std::uint64_t dropped_frames = 0;
    std::uint64_t recoveries = 0;
    std::uint64_t short_reads = 0;
    std::uint64_t processing_failures = 0;
    std::uint64_t resets = 0;
};

class FrameObserver {
   public:
    virtual ~FrameObserver() = default;

    virtual void OnFrame(
        const std::int16_t* raw_mic,
        const std::int16_t* raw_reference,
        const std::int16_t* processed_mic,
        std::size_t frames) = 0;
};

class CaptureEngine {
   public:
    explicit CaptureEngine(CaptureConfig config, FrameObserver* observer = nullptr);
    ~CaptureEngine();

    CaptureEngine(const CaptureEngine&) = delete;
    CaptureEngine& operator=(const CaptureEngine&) = delete;

    int Start();
    void Stop();
    int Read(
        std::int16_t* output,
        std::size_t frames,
        std::chrono::milliseconds timeout);
    int RequestReset();

    CaptureStats GetStats() const;
    std::string LastError() const;

   private:
    bool ValidateConfig(std::string* error) const;
    bool OpenAlsa(std::string* error);
    bool BuildProcessor(std::string* error);
    void ReleaseProcessor();
    bool ResetProcessor(std::string* error);
    void WorkerLoop();
    void WorkerLoopImpl();
    bool Publish(const std::int16_t* samples, std::size_t frames);
    void SetTerminalError(int status, const std::string& error);
    void CloseAlsa();

    CaptureConfig config_;
    FrameObserver* observer_;

    mutable std::mutex mutex_;
    std::condition_variable data_ready_;
    std::condition_variable first_frame_ready_;
    std::vector<std::int16_t> ring_;
    std::size_t ring_read_ = 0;
    std::size_t ring_write_ = 0;
    std::size_t ring_size_ = 0;
    CaptureStats stats_;
    std::string last_error_;
    int terminal_status_ = 0;
    bool running_ = false;
    bool first_frame_seen_ = false;

    std::atomic<bool> stop_requested_{false};
    std::atomic<bool> reset_requested_{false};
    std::thread worker_;

    snd_pcm_t* pcm_ = nullptr;
    webrtc::AudioProcessing* processor_ = nullptr;
    webrtc::StreamConfig* stream_config_ = nullptr;
};

}  // namespace codex::aec3
