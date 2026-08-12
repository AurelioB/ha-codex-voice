#include <signal.h>

#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <limits>
#include <string>
#include <string_view>
#include <vector>

#include "capture_engine.h"
#include "codex_aec3_capture.h"

namespace {

std::atomic<bool> stop_requested{false};

extern "C" void HandleSignal(int) {
    stop_requested.store(true, std::memory_order_relaxed);
}

void WriteLe16(std::uint8_t* output, std::uint16_t value) {
    output[0] = static_cast<std::uint8_t>(value & 0xffu);
    output[1] = static_cast<std::uint8_t>((value >> 8u) & 0xffu);
}

void WriteLe32(std::uint8_t* output, std::uint32_t value) {
    output[0] = static_cast<std::uint8_t>(value & 0xffu);
    output[1] = static_cast<std::uint8_t>((value >> 8u) & 0xffu);
    output[2] = static_cast<std::uint8_t>((value >> 16u) & 0xffu);
    output[3] = static_cast<std::uint8_t>((value >> 24u) & 0xffu);
}

class WavWriter {
   public:
    ~WavWriter() {
        Close();
    }

    bool Open(const std::filesystem::path& path, std::string* error) {
        file_ = std::fopen(path.c_str(), "wb");
        if (file_ == nullptr) {
            *error = path.string() + ": " + std::strerror(errno);
            return false;
        }
        std::uint8_t header[44]{};
        if (std::fwrite(header, 1, sizeof(header), file_) != sizeof(header)) {
            *error = path.string() + ": could not reserve WAV header";
            Close();
            return false;
        }
        path_ = path.string();
        return true;
    }

    bool Write(const std::int16_t* samples, std::size_t frames) {
        if (file_ == nullptr || failed_) {
            return false;
        }
        if (std::fwrite(samples, sizeof(*samples), frames, file_) != frames) {
            failed_ = true;
            return false;
        }
        frames_ += frames;
        return true;
    }

    bool failed() const {
        return failed_;
    }

    const std::string& path() const {
        return path_;
    }

    void Close() {
        if (file_ == nullptr) {
            return;
        }
        const std::uint64_t byte_count_64 = frames_ * sizeof(std::int16_t);
        const std::uint32_t byte_count = static_cast<std::uint32_t>(
            std::min<std::uint64_t>(
                byte_count_64, std::numeric_limits<std::uint32_t>::max() - 36u));
        std::uint8_t header[44]{};
        std::memcpy(header, "RIFF", 4);
        WriteLe32(header + 4, 36u + byte_count);
        std::memcpy(header + 8, "WAVEfmt ", 8);
        WriteLe32(header + 16, 16u);
        WriteLe16(header + 20, 1u);
        WriteLe16(header + 22, 1u);
        WriteLe32(header + 24, codex::aec3::kAec3SampleRate);
        WriteLe32(header + 28, codex::aec3::kAec3SampleRate * 2u);
        WriteLe16(header + 32, 2u);
        WriteLe16(header + 34, 16u);
        std::memcpy(header + 36, "data", 4);
        WriteLe32(header + 40, byte_count);
        if (std::fseek(file_, 0, SEEK_SET) != 0 ||
            std::fwrite(header, 1, sizeof(header), file_) != sizeof(header)) {
            failed_ = true;
        }
        if (std::fclose(file_) != 0) {
            failed_ = true;
        }
        file_ = nullptr;
    }

   private:
    std::FILE* file_ = nullptr;
    std::uint64_t frames_ = 0;
    std::string path_;
    bool failed_ = false;
};

double SumSquares(const std::int16_t* samples, std::size_t frames) {
    double energy = 0.0;
    for (std::size_t frame = 0; frame < frames; ++frame) {
        const double value = samples[frame];
        energy += value * value;
    }
    return energy;
}

class CanaryObserver final : public codex::aec3::FrameObserver {
   public:
    bool Open(const std::filesystem::path& output, std::string* error) {
        std::error_code filesystem_error;
        std::filesystem::create_directories(output, filesystem_error);
        if (filesystem_error) {
            *error = output.string() + ": " + filesystem_error.message();
            return false;
        }
        return raw_mic_.Open(output / "raw_mic.wav", error) &&
            reference_.Open(output / "reference.wav", error) &&
            processed_.Open(output / "aec3_output.wav", error);
    }

    void OnFrame(
        const std::int16_t* raw_mic,
        const std::int16_t* raw_reference,
        const std::int16_t* processed_mic,
        std::size_t frames) override {
        if (!raw_mic_.Write(raw_mic, frames) ||
            !reference_.Write(raw_reference, frames) ||
            !processed_.Write(processed_mic, frames)) {
            failed_.store(true, std::memory_order_relaxed);
            return;
        }

        reference_energy_ += SumSquares(raw_reference, frames);
        raw_energy_ += SumSquares(raw_mic, frames);
        processed_energy_ += SumSquares(processed_mic, frames);
        window_frames_ += frames;
        if (window_frames_ < codex::aec3::kAec3SampleRate) {
            return;
        }

        const double reference_rms =
            std::sqrt(reference_energy_ / static_cast<double>(window_frames_));
        const double raw_rms =
            std::sqrt(raw_energy_ / static_cast<double>(window_frames_));
        const double processed_rms =
            std::sqrt(processed_energy_ / static_cast<double>(window_frames_));
        const bool render_active = reference_rms >= 64.0;
        const double erle_db = render_active
            ? 10.0 * std::log10(
                  (raw_energy_ + 1.0) / (processed_energy_ + 1.0))
            : 0.0;
        ++second_;
        std::printf(
            "second=%llu render_active=%d ref_rms=%.1f raw_rms=%.1f "
            "aec3_rms=%.1f erle_db=%.2f\n",
            static_cast<unsigned long long>(second_), render_active ? 1 : 0,
            reference_rms, raw_rms, processed_rms, erle_db);
        std::fflush(stdout);
        reference_energy_ = 0.0;
        raw_energy_ = 0.0;
        processed_energy_ = 0.0;
        window_frames_ = 0;
    }

    bool failed() const {
        return failed_.load(std::memory_order_relaxed) || raw_mic_.failed() ||
            reference_.failed() || processed_.failed();
    }

    void Close() {
        raw_mic_.Close();
        reference_.Close();
        processed_.Close();
    }

   private:
    WavWriter raw_mic_;
    WavWriter reference_;
    WavWriter processed_;
    std::atomic<bool> failed_{false};
    std::uint64_t second_ = 0;
    std::size_t window_frames_ = 0;
    double reference_energy_ = 0.0;
    double raw_energy_ = 0.0;
    double processed_energy_ = 0.0;
};

struct Options {
    codex::aec3::CaptureConfig capture;
    std::filesystem::path output = "/tmp/codex-aec3-canary";
    unsigned duration_seconds = 15;
};

void PrintUsage(const char* program) {
    std::fprintf(
        stderr,
        "Usage: %s [options]\n\n"
        "  --out DIR                 WAV output directory\n"
        "  --duration SECONDS        capture duration (1..300; default 15)\n"
        "  --device ALSA_DEVICE      default hw:0,4\n"
        "  --mic-channel INDEX       default 0\n"
        "  --reference-a INDEX       default 2\n"
        "  --reference-b INDEX       default 3; -1 uses one reference\n"
        "  --startup-timeout-ms MS   first-frame timeout (default 1000)\n"
        "  --help                    show this text\n\n"
        "Start speaker playback through the existing PulseAudio raw sink before "
        "running this canary. The program never restarts or reconfigures PulseAudio.\n",
        program);
}

bool ParseInteger(
    const char* value,
    long minimum,
    long maximum,
    long* output) {
    errno = 0;
    char* end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed < minimum ||
        parsed > maximum) {
        return false;
    }
    *output = parsed;
    return true;
}

bool NextArgument(int argc, char** argv, int* index, const char** output) {
    if (*index + 1 >= argc) {
        return false;
    }
    ++*index;
    *output = argv[*index];
    return true;
}

bool ParseOptions(int argc, char** argv, Options* options) {
    for (int index = 1; index < argc; ++index) {
        const std::string_view argument(argv[index]);
        if (argument == "--help") {
            PrintUsage(argv[0]);
            std::exit(0);
        }

        const char* value = nullptr;
        if (!NextArgument(argc, argv, &index, &value)) {
            std::fprintf(stderr, "%.*s requires a value\n",
                         static_cast<int>(argument.size()), argument.data());
            return false;
        }
        long parsed = 0;
        if (argument == "--out") {
            options->output = value;
        } else if (argument == "--device") {
            options->capture.alsa_device = value;
        } else if (argument == "--duration") {
            if (!ParseInteger(value, 1, 300, &parsed)) {
                return false;
            }
            options->duration_seconds = static_cast<unsigned>(parsed);
        } else if (argument == "--mic-channel") {
            if (!ParseInteger(value, 0, 31, &parsed)) {
                return false;
            }
            options->capture.mic_channel = static_cast<unsigned>(parsed);
        } else if (argument == "--reference-a") {
            if (!ParseInteger(value, 0, 31, &parsed)) {
                return false;
            }
            options->capture.reference_channel_a = static_cast<int>(parsed);
        } else if (argument == "--reference-b") {
            if (!ParseInteger(value, -1, 31, &parsed)) {
                return false;
            }
            options->capture.reference_channel_b = static_cast<int>(parsed);
        } else if (argument == "--startup-timeout-ms") {
            if (!ParseInteger(value, 100, 10'000, &parsed)) {
                return false;
            }
            options->capture.startup_timeout =
                std::chrono::milliseconds(parsed);
        } else {
            std::fprintf(stderr, "unknown option: %.*s\n",
                         static_cast<int>(argument.size()), argument.data());
            return false;
        }
    }
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    Options options;
    if (!ParseOptions(argc, argv, &options)) {
        PrintUsage(argv[0]);
        return 2;
    }

    struct sigaction action {};
    action.sa_handler = HandleSignal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, nullptr);
    sigaction(SIGTERM, &action, nullptr);

    CanaryObserver observer;
    std::string error;
    if (!observer.Open(options.output, &error)) {
        std::fprintf(stderr, "output error: %s\n", error.c_str());
        return 3;
    }

    codex::aec3::CaptureEngine engine(options.capture, &observer);
    const int start_status = engine.Start();
    if (start_status != CODEX_AEC3_OK) {
        std::fprintf(
            stderr, "AEC3 capture start failed (%d): %s\n", start_status,
            engine.LastError().c_str());
        return 4;
    }

    const std::uint64_t target_frames =
        static_cast<std::uint64_t>(options.duration_seconds) *
        codex::aec3::kAec3SampleRate;
    std::uint64_t received = 0;
    std::vector<std::int16_t> discard(codex::aec3::kAec3FrameSamples);
    int read_status = 0;
    while (!stop_requested.load(std::memory_order_relaxed) &&
           received < target_frames && !observer.failed()) {
        read_status = engine.Read(
            discard.data(), discard.size(), std::chrono::milliseconds(500));
        if (read_status == CODEX_AEC3_TIMEOUT) {
            continue;
        }
        if (read_status < 0) {
            break;
        }
        received += static_cast<std::uint64_t>(read_status);
    }

    engine.Stop();
    observer.Close();
    const auto stats = engine.GetStats();
    std::printf(
        "summary captured=%llu delivered=%llu dropped=%llu recoveries=%llu "
        "short_reads=%llu processing_failures=%llu resets=%llu\n",
        static_cast<unsigned long long>(stats.captured_frames),
        static_cast<unsigned long long>(stats.delivered_frames),
        static_cast<unsigned long long>(stats.dropped_frames),
        static_cast<unsigned long long>(stats.recoveries),
        static_cast<unsigned long long>(stats.short_reads),
        static_cast<unsigned long long>(stats.processing_failures),
        static_cast<unsigned long long>(stats.resets));

    if (observer.failed()) {
        std::fprintf(stderr, "WAV output failed\n");
        return 5;
    }
    if (read_status < 0 && read_status != CODEX_AEC3_TIMEOUT) {
        std::fprintf(
            stderr, "AEC3 capture failed (%d): %s\n", read_status,
            engine.LastError().c_str());
        return 6;
    }
    return 0;
}
