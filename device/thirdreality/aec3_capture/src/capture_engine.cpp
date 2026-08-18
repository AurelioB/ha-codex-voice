#include "capture_engine.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <utility>

#include "codex_aec3_capture.h"
#include "modules/audio_processing/include/audio_processing.h"

namespace codex::aec3 {

namespace {

// Keep ThirdReality's newer 10 dB native baseline, then let AGC2 adapt for
// distance without raising the output noise floor above -50 dBFS. Gain belongs
// inside APM so its limiter protects close speech and the vendor wake detector
// receives the same conditioned samples as realtime capture.
constexpr float kFixedCaptureGainDb = 10.0F;
constexpr float kMaximumOutputNoiseDbfs = -50.0F;

std::string AlsaError(const char* operation, int error) {
    return std::string(operation) + ": " + snd_strerror(error);
}

}  // namespace

CaptureEngine::CaptureEngine(CaptureConfig config, FrameObserver* observer)
    : config_(std::move(config)), observer_(observer) {}

CaptureEngine::~CaptureEngine() {
    Stop();
}

bool CaptureEngine::ValidateConfig(std::string* error) const {
    if (config_.alsa_device.empty()) {
        *error = "ALSA capture device must not be empty";
        return false;
    }
    if (config_.sample_rate != kAec3SampleRate) {
        *error = "AEC3 capture requires exactly 16000 Hz";
        return false;
    }
    if (config_.period_frames != kAec3FrameSamples) {
        *error = "AEC3 capture requires exactly 160 frames per period";
        return false;
    }
    if (config_.channels == 0 || config_.channels > 32 ||
        config_.mic_channel >= config_.channels) {
        *error = "microphone channel is outside the capture layout";
        return false;
    }
    if (config_.secondary_mic_channel < -1 ||
        (config_.secondary_mic_channel >= 0 &&
         static_cast<unsigned>(config_.secondary_mic_channel) >=
             config_.channels)) {
        *error = "secondary microphone channel is outside the capture layout";
        return false;
    }
    if (config_.secondary_mic_channel ==
        static_cast<int>(config_.mic_channel)) {
        *error = "primary and secondary microphone channels must be distinct";
        return false;
    }
    if (config_.reference_channel_a < 0 ||
        static_cast<unsigned>(config_.reference_channel_a) >= config_.channels) {
        *error = "first reference channel is outside the capture layout";
        return false;
    }
    if (config_.reference_channel_b < -1) {
        *error = "second reference channel must be -1 or a channel index";
        return false;
    }
    if (config_.reference_channel_b >= 0 &&
        static_cast<unsigned>(config_.reference_channel_b) >= config_.channels) {
        *error = "second reference channel is outside the capture layout";
        return false;
    }
    if (config_.reference_channel_a ==
        static_cast<int>(config_.mic_channel)) {
        *error = "microphone and reference channels must be distinct";
        return false;
    }
    if (config_.reference_channel_b ==
        static_cast<int>(config_.mic_channel)) {
        *error = "microphone and reference channels must be distinct";
        return false;
    }
    if (config_.secondary_mic_channel == config_.reference_channel_a ||
        config_.secondary_mic_channel == config_.reference_channel_b) {
        *error = "secondary microphone and reference channels must be distinct";
        return false;
    }
    if (config_.reference_channel_b == config_.reference_channel_a) {
        *error = "reference channels must be distinct";
        return false;
    }
    if (config_.ring_frames < config_.period_frames * 2) {
        *error = "capture ring must hold at least two AEC3 periods";
        return false;
    }
    if (config_.ring_frames > config_.sample_rate * 2) {
        *error = "capture ring must not exceed two seconds";
        return false;
    }
    if (config_.startup_timeout.count() < 100 ||
        config_.startup_timeout.count() > 10'000) {
        *error = "startup timeout must be in 100..10000 ms";
        return false;
    }
    return true;
}

bool CaptureEngine::OpenAlsa(std::string* error) {
    snd_pcm_t* pcm = nullptr;
    int result = snd_pcm_open(
        &pcm, config_.alsa_device.c_str(), SND_PCM_STREAM_CAPTURE, 0);
    if (result < 0) {
        *error = AlsaError("snd_pcm_open", result);
        return false;
    }

    snd_pcm_hw_params_t* params = nullptr;
    snd_pcm_hw_params_alloca(&params);

    auto fail = [&](const char* operation, int code) {
        *error = AlsaError(operation, code);
        snd_pcm_close(pcm);
        return false;
    };

    if ((result = snd_pcm_hw_params_any(pcm, params)) < 0) {
        return fail("snd_pcm_hw_params_any", result);
    }
    if ((result = snd_pcm_hw_params_set_access(
             pcm, params, SND_PCM_ACCESS_RW_INTERLEAVED)) < 0) {
        return fail("snd_pcm_hw_params_set_access", result);
    }
    if ((result = snd_pcm_hw_params_set_format(
             pcm, params, SND_PCM_FORMAT_S16_LE)) < 0) {
        return fail("snd_pcm_hw_params_set_format", result);
    }
    if ((result = snd_pcm_hw_params_set_channels(
             pcm, params, config_.channels)) < 0) {
        return fail("snd_pcm_hw_params_set_channels", result);
    }

    unsigned rate = config_.sample_rate;
    int direction = 0;
    if ((result = snd_pcm_hw_params_set_rate_near(
             pcm, params, &rate, &direction)) < 0) {
        return fail("snd_pcm_hw_params_set_rate_near", result);
    }
    if (rate != config_.sample_rate) {
        *error = "ALSA did not accept the exact 16000 Hz capture rate";
        snd_pcm_close(pcm);
        return false;
    }

    snd_pcm_uframes_t period = config_.period_frames;
    direction = 0;
    if ((result = snd_pcm_hw_params_set_period_size_near(
             pcm, params, &period, &direction)) < 0) {
        return fail("snd_pcm_hw_params_set_period_size_near", result);
    }
    if (period != config_.period_frames) {
        *error = "ALSA did not accept the exact 160-frame AEC3 period";
        snd_pcm_close(pcm);
        return false;
    }

    snd_pcm_uframes_t buffer_frames = config_.sample_rate / 2;
    if ((result = snd_pcm_hw_params_set_buffer_size_near(
             pcm, params, &buffer_frames)) < 0) {
        return fail("snd_pcm_hw_params_set_buffer_size_near", result);
    }
    if ((result = snd_pcm_hw_params(pcm, params)) < 0) {
        return fail("snd_pcm_hw_params", result);
    }

    pcm_ = pcm;
    return true;
}

bool CaptureEngine::BuildProcessor(std::string* error) {
    auto* processor = webrtc::AudioProcessingBuilder().Create();
    if (processor == nullptr) {
        *error = "AudioProcessingBuilder::Create returned null";
        return false;
    }

    auto* stream = new (std::nothrow)
        webrtc::StreamConfig(config_.sample_rate, 1, false);
    if (stream == nullptr) {
        processor->Release();
        *error = "could not allocate the AEC3 stream configuration";
        return false;
    }

    webrtc::AudioProcessing::Config processing_config;
    processing_config.echo_canceller.enabled = true;
    processing_config.echo_canceller.mobile_mode = false;
    processing_config.echo_canceller.enforce_high_pass_filtering = true;
    processing_config.high_pass_filter.enabled = true;
    processing_config.gain_controller2.enabled = true;
    processing_config.gain_controller2.fixed_digital.gain_db =
        kFixedCaptureGainDb;
    processing_config.gain_controller2.adaptive_digital.enabled = true;
    processing_config.gain_controller2.adaptive_digital
        .max_output_noise_level_dbfs = kMaximumOutputNoiseDbfs;
    processing_config.noise_suppression.enabled = true;
    processing_config.noise_suppression.level =
        webrtc::AudioProcessing::Config::NoiseSuppression::kModerate;
    processor->ApplyConfig(processing_config);

    processor_ = processor;
    stream_config_ = stream;
    return true;
}

void CaptureEngine::ReleaseProcessor() {
    if (processor_ != nullptr) {
        processor_->Release();
        processor_ = nullptr;
    }
    delete stream_config_;
    stream_config_ = nullptr;
}

bool CaptureEngine::ResetProcessor(std::string* error) {
    ReleaseProcessor();
    if (!BuildProcessor(error)) {
        return false;
    }
    microphone_combiner_.Reset();
    std::lock_guard<std::mutex> lock(mutex_);
    ++stats_.resets;
    return true;
}

int CaptureEngine::Start() {
    std::string error;
    if (!ValidateConfig(&error)) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = error;
        return CODEX_AEC3_INVALID_ARGUMENT;
    }

    try {
        ring_.assign(config_.ring_frames, 0);
    } catch (const std::bad_alloc&) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = "could not allocate the bounded capture ring";
        return CODEX_AEC3_INTERNAL_ERROR;
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (running_) {
            last_error_ = "capture is already running";
            return CODEX_AEC3_INVALID_STATE;
        }
        ring_read_ = 0;
        ring_write_ = 0;
        ring_size_ = 0;
        stats_ = {};
        last_error_.clear();
        terminal_status_ = 0;
        first_frame_seen_ = false;
    }

    if (!OpenAlsa(&error)) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = error;
        return CODEX_AEC3_ALSA_ERROR;
    }
    if (!BuildProcessor(&error)) {
        CloseAlsa();
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = error;
        return CODEX_AEC3_PROCESSING_ERROR;
    }
    microphone_combiner_.Reset();

    stop_requested_.store(false, std::memory_order_release);
    reset_requested_.store(false, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        running_ = true;
    }
    try {
        worker_ = std::thread([this] { WorkerLoop(); });
    } catch (...) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            running_ = false;
            last_error_ = "could not start the capture worker";
        }
        ReleaseProcessor();
        CloseAlsa();
        return CODEX_AEC3_INTERNAL_ERROR;
    }

    std::unique_lock<std::mutex> lock(mutex_);
    const bool ready = first_frame_ready_.wait_for(
        lock, config_.startup_timeout,
        [this] { return first_frame_seen_ || terminal_status_ != 0; });
    if (ready && first_frame_seen_) {
        return CODEX_AEC3_OK;
    }
    const int status = terminal_status_ != 0
        ? terminal_status_
        : CODEX_AEC3_TIMEOUT;
    if (!ready) {
        last_error_ =
            "no hardware-loopback frame arrived before startup timeout; "
            "start playback DMA before opening hw:0,4";
    }
    lock.unlock();
    Stop();
    return status;
}

void CaptureEngine::Stop() {
    bool was_running = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        was_running = running_;
    }
    if (!was_running && !worker_.joinable()) {
        ReleaseProcessor();
        CloseAlsa();
        return;
    }

    stop_requested_.store(true, std::memory_order_release);
    data_ready_.notify_all();
    first_frame_ready_.notify_all();

    // ALSA documents snd_pcm_abort for interrupting a blocked operation from
    // another thread. The capture thread remains the only read caller.
    if (pcm_ != nullptr) {
        snd_pcm_abort(pcm_);
    }
    if (worker_.joinable()) {
        worker_.join();
    }

    ReleaseProcessor();
    CloseAlsa();
    {
        std::lock_guard<std::mutex> lock(mutex_);
        running_ = false;
    }
}

int CaptureEngine::Read(
    std::int16_t* output,
    std::size_t frames,
    std::chrono::milliseconds timeout) {
    if (output == nullptr || frames == 0 ||
        frames > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
        timeout.count() < 0) {
        return CODEX_AEC3_INVALID_ARGUMENT;
    }

    std::unique_lock<std::mutex> lock(mutex_);
    if (!running_) {
        return CODEX_AEC3_INVALID_STATE;
    }
    const bool ready = data_ready_.wait_for(lock, timeout, [this] {
        return ring_size_ != 0 || terminal_status_ != 0 ||
            stop_requested_.load(std::memory_order_acquire);
    });
    if (!ready) {
        return CODEX_AEC3_TIMEOUT;
    }
    if (ring_size_ == 0) {
        return terminal_status_ != 0 ? terminal_status_ : CODEX_AEC3_STOPPED;
    }

    const std::size_t count = std::min(frames, ring_size_);
    const std::size_t first = std::min(count, ring_.size() - ring_read_);
    std::copy_n(ring_.data() + ring_read_, first, output);
    if (first < count) {
        std::copy_n(ring_.data(), count - first, output + first);
    }
    ring_read_ = (ring_read_ + count) % ring_.size();
    ring_size_ -= count;
    stats_.delivered_frames += count;
    return static_cast<int>(count);
}

int CaptureEngine::RequestReset() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!running_ || terminal_status_ != 0) {
        return CODEX_AEC3_INVALID_STATE;
    }
    reset_requested_.store(true, std::memory_order_release);
    return CODEX_AEC3_OK;
}

CaptureStats CaptureEngine::GetStats() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

std::string CaptureEngine::LastError() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return last_error_;
}

bool CaptureEngine::Publish(const std::int16_t* samples, std::size_t frames) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (ring_.size() - ring_size_ < frames) {
        stats_.dropped_frames += frames;
        terminal_status_ = CODEX_AEC3_RING_OVERFLOW;
        last_error_ = "processed capture ring overflowed";
        first_frame_ready_.notify_all();
        data_ready_.notify_all();
        return false;
    }

    const std::size_t first = std::min(frames, ring_.size() - ring_write_);
    std::copy_n(samples, first, ring_.data() + ring_write_);
    if (first < frames) {
        std::copy_n(samples + first, frames - first, ring_.data());
    }
    ring_write_ = (ring_write_ + frames) % ring_.size();
    ring_size_ += frames;
    stats_.captured_frames += frames;
    first_frame_seen_ = true;
    first_frame_ready_.notify_all();
    data_ready_.notify_one();
    return true;
}

void CaptureEngine::SetTerminalError(int status, const std::string& error) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (terminal_status_ == 0) {
        terminal_status_ = status;
        last_error_ = error;
    }
    first_frame_ready_.notify_all();
    data_ready_.notify_all();
}

void CaptureEngine::WorkerLoop() {
    try {
        WorkerLoopImpl();
    } catch (const std::exception& error) {
        SetTerminalError(
            CODEX_AEC3_INTERNAL_ERROR,
            std::string("capture worker exception: ") + error.what());
    } catch (...) {
        SetTerminalError(
            CODEX_AEC3_INTERNAL_ERROR, "unknown capture worker exception");
    }
}

void CaptureEngine::WorkerLoopImpl() {
    const std::size_t period = config_.period_frames;
    std::vector<std::int16_t> interleaved(period * config_.channels);
    std::vector<std::int16_t> primary_mic(period);
    std::vector<std::int16_t> secondary_mic(period);
    std::vector<std::int16_t> raw_mic(period);
    std::vector<std::int16_t> raw_reference(period);
    std::vector<std::int16_t> apm_reference(period);
    std::vector<std::int16_t> processed_mic(period);

    while (!stop_requested_.load(std::memory_order_acquire)) {
        if (reset_requested_.exchange(false, std::memory_order_acq_rel)) {
            std::string reset_error;
            if (!ResetProcessor(&reset_error)) {
                SetTerminalError(CODEX_AEC3_PROCESSING_ERROR, reset_error);
                break;
            }
        }

        const snd_pcm_sframes_t read =
            snd_pcm_readi(pcm_, interleaved.data(), period);
        if (read < 0) {
            const int error = static_cast<int>(read);
            if (stop_requested_.load(std::memory_order_acquire)) {
                break;
            }
            if (error == -EPIPE || error == -ESTRPIPE || error == -EINTR ||
                error == -EIO) {
                const int recovered = snd_pcm_recover(pcm_, error, 1);
                if (recovered < 0) {
                    SetTerminalError(
                        CODEX_AEC3_ALSA_ERROR,
                        AlsaError("snd_pcm_recover", recovered));
                    break;
                }
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.recoveries;
                }
                std::string reset_error;
                if (!ResetProcessor(&reset_error)) {
                    SetTerminalError(CODEX_AEC3_PROCESSING_ERROR, reset_error);
                    break;
                }
                continue;
            }
            SetTerminalError(
                CODEX_AEC3_ALSA_ERROR, AlsaError("snd_pcm_readi", error));
            break;
        }

        if (static_cast<std::size_t>(read) != period) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                ++stats_.short_reads;
            }
            std::string reset_error;
            if (!ResetProcessor(&reset_error)) {
                SetTerminalError(CODEX_AEC3_PROCESSING_ERROR, reset_error);
                break;
            }
            continue;
        }

        for (std::size_t frame = 0; frame < period; ++frame) {
            const std::int16_t* row =
                interleaved.data() + frame * config_.channels;
            primary_mic[frame] = row[config_.mic_channel];
            if (config_.secondary_mic_channel >= 0) {
                secondary_mic[frame] = row[config_.secondary_mic_channel];
            }
            if (config_.reference_channel_b < 0) {
                raw_reference[frame] = row[config_.reference_channel_a];
            } else {
                const std::int32_t mixed =
                    static_cast<std::int32_t>(row[config_.reference_channel_a]) +
                    static_cast<std::int32_t>(row[config_.reference_channel_b]);
                raw_reference[frame] = static_cast<std::int16_t>(mixed / 2);
            }
        }

        if (config_.secondary_mic_channel >= 0) {
            const auto combined = microphone_combiner_.Process(
                primary_mic.data(), secondary_mic.data(), raw_mic.data(),
                period);
            std::lock_guard<std::mutex> lock(mutex_);
            if (combined.coherent) {
                stats_.coherent_mic_frames += period;
            } else {
                stats_.primary_only_mic_frames += period;
            }
        } else {
            raw_mic = primary_mic;
            std::lock_guard<std::mutex> lock(mutex_);
            stats_.primary_only_mic_frames += period;
        }

        apm_reference = raw_reference;
        processed_mic = raw_mic;
        const int reverse_status = processor_->ProcessReverseStream(
            apm_reference.data(), *stream_config_, *stream_config_,
            apm_reference.data());
        processor_->set_stream_delay_ms(0);
        const int capture_status = processor_->ProcessStream(
            processed_mic.data(), *stream_config_, *stream_config_,
            processed_mic.data());
        if (reverse_status != webrtc::AudioProcessing::kNoError ||
            capture_status != webrtc::AudioProcessing::kNoError) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                ++stats_.processing_failures;
            }
            SetTerminalError(
                CODEX_AEC3_PROCESSING_ERROR,
                "WebRTC APM rejected a synchronized 10 ms frame");
            break;
        }

        if (observer_ != nullptr) {
            observer_->OnFrame(
                raw_mic.data(), raw_reference.data(), processed_mic.data(),
                period);
        }
        if (!Publish(processed_mic.data(), period)) {
            break;
        }
    }

    data_ready_.notify_all();
    first_frame_ready_.notify_all();
}

void CaptureEngine::CloseAlsa() {
    if (pcm_ != nullptr) {
        snd_pcm_close(pcm_);
        pcm_ = nullptr;
    }
}

}  // namespace codex::aec3
