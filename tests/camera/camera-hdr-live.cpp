// SPDX-License-Identifier: MIT
// Continuous direct-IFE bracket source. stdout carries exposure metadata and
// either the shared NV12 slot number or inline pixels for pipe consumers.
#include <libcamera/libcamera.h>
#include <linux/dma-buf.h>
#include <linux/videodev2.h>
#include <opencv2/core.hpp>
#include <libyuv/scale.h>
#include <libyuv/scale_uv.h>
#include <optional>
#include <thread>
#include <vector>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <stdexcept>

using namespace libcamera;
namespace fs = std::filesystem;

static uint64_t now() {
    timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t))
        throw std::runtime_error("clock");
    return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}

/* The recorder returns a one-byte slot number only after registration has
 * finished reading all three exposures. Partial brackets keep the same slot. */
class BracketSlots {
  public:
    void map(int fd, int credits, size_t frameBytes) {
        struct stat st{};
        int flags = fcntl(credits, F_GETFL);
        size_t groupBytes = 3 * frameBytes;
        if (flags < 0 || !(flags & O_NONBLOCK) || fstat(fd, &st) ||
            st.st_size <= 0 || size_t(st.st_size) % groupBytes ||
            size_t(st.st_size) / groupBytes > 4)
            throw std::runtime_error("invalid shared bracket storage");
        length_ = st.st_size;
        void *addr = mmap(nullptr, length_, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, fd, 0);
        if (addr == MAP_FAILED) throw std::runtime_error("map shared brackets");
        base_ = static_cast<uint8_t *>(addr);
        frameBytes_ = frameBytes;
        credits_ = credits;
    }
    ~BracketSlots() {
        if (base_) munmap(base_, length_);
    }
    bool enabled() const { return base_ != nullptr; }
    bool acquire() {
        if (slot >= 0) return true;
        uint8_t token;
        ssize_t n;
        do { n = read(credits_, &token, 1); } while (n < 0 && errno == EINTR);
        if (n < 0 && errno == EAGAIN) return false;
        if (n != 1 || token >= length_ / (3 * frameBytes_))
            throw std::runtime_error("invalid shared bracket credit");
        slot = token;
        return true;
    }
    uint8_t *image(int leg) const {
        return base_ + (3 * slot + leg) * frameBytes_;
    }
    int slot = -1;

  private:
    uint8_t *base_ = nullptr;
    int credits_ = -1;
    size_t length_ = 0, frameBytes_ = 0;
};

class Source {
  public:
    std::shared_ptr<Camera> cam;
    Stream *stream{};
    Size output{640, 480};
    std::array<int, 3> timesUs{2000, 8000, 32000};
    uint64_t period = 33334000, writeWindow = 12000000;
    bool rear = false;
    bool requireInterleaved = false;
    bool traceStartup = false;
    bool traceTail = false;
    StreamConfiguration format{};
    BracketSlots slots;
    int sensor = -1, role = 0, lines = 0, groups = 0, seconds = 0;
    double lineUs = 0;
    float gain = 1;
    std::array<float, 2> wb{1, 1};
    std::array<float, 9> ccm{};
    bool haveCcm = false;
    std::optional<float> settledFocus;
    struct Mapping {
        int fd;
        void *base;
        size_t size;
    };
    std::vector<Mapping> mappings;
    ~Source() {
        finishWriter();
        for (const auto &m : mappings) munmap(m.base, m.size);
        if (cam)
            cam->release();
        if (sensor >= 0)
            close(sensor);
    }
    int64_t get(uint32_t id) {
        v4l2_ext_control c{};
        c.id = id;
        v4l2_ext_controls cs{};
        cs.count = 1;
        cs.controls = &c;
        if (ioctl(sensor, VIDIOC_G_EXT_CTRLS, &cs))
            throw std::runtime_error("sensor control read");
        return id == V4L2_CID_PIXEL_RATE ? c.value64 : c.value;
    }
    struct Write {
        uint32_t sequence;
        uint64_t frameTime, end;
        int leg, lines;
        bool timely(uint64_t writeWindow) const { return end >= frameTime && end - frameTime <= writeWindow; }
    };
    using Pixels = std::array<cv::Mat, 2>;
    struct Selected {
        Write command;
        uint32_t seq;
        uint64_t time;
        Pixels pixels;
        int delay;
    };
    Write change(int leg) {
        if (lines && get(V4L2_CID_EXPOSURE) != lines)
            throw std::runtime_error("another exposure writer");
        lines = std::lround(timesUs[leg] / lineUs);
        v4l2_control c{};
        c.id = V4L2_CID_EXPOSURE;
        c.value = lines;
        if (ioctl(sensor, VIDIOC_S_CTRL, &c) || get(c.id) != lines)
            throw std::runtime_error("exposure programming");
        return {prev, last, now(), leg, lines};
    }

    void frame(Request *r) {
        std::lock_guard<std::mutex> lock(mutex);
        if (done || r->status() == Request::RequestCancelled)
            return;
        try {
            auto *buf = r->findBuffer(stream);
            if (!buf || buf->metadata().status != FrameMetadata::FrameSuccess)
                throw std::runtime_error("incomplete buffer");
            auto &m = buf->metadata();
            auto ts = r->metadata().get(controls::SensorTimestamp);
            if (!ts || uint64_t(*ts) != m.timestamp ||
                (count && (m.timestamp <= last || m.sequence <= prev)))
                throw std::runtime_error("direct IFE sequence/timestamp discontinuity");
            if (!warm && count && m.sequence != prev + 1)
                throw std::runtime_error("lost IFE frame during bracket");
            ++count;
            prev = m.sequence;
            last = m.timestamp;
            if (warm) {
                if (auto position = r->metadata().get(controls::LensPosition))
                    settledFocus = *position;
                if (auto g = r->metadata().get(controls::AnalogueGain))
                    gain = *g;
                if (auto w = r->metadata().get(controls::ColourGains))
                    wb = {(*w)[0], (*w)[1]};
                if (auto c = r->metadata().get(controls::ColourCorrectionMatrix)) {
                    std::copy(c->begin(), c->end(), ccm.begin());
                    haveCcm = true;
                }
                if (count == 65) {
                    done = true;
                    cv.notify_one();
                    return;
                }
            } else if (bracket(buf)) {
                done = true;
                cv.notify_one();
                return;
            }
            r->reuse(Request::ReuseBuffers);
            if (cam->queueRequest(r))
                throw std::runtime_error("request queue");
        } catch (const std::exception &e) {
            error = e.what();
            std::cerr << error << " at frame " << count << "\n";
            done = true;
            cv.notify_one();
        }
    }

    void run(std::vector<std::unique_ptr<Request>> &reqs, ControlList &ctl, bool automatic) {
        if (!automatic && (traceStartup || traceTail)) {
            // At 60 Hz a selected frame can produce two capture events.
            const int window = (traceStartup ? 2 : 0) + (traceTail ? 62 : 0);
            events.reserve(120 * std::min(seconds, window) + 8);
        }
        {
            std::lock_guard<std::mutex> lock(mutex);
            warm = automatic;
            count = 0;
            done = false;
            error.clear();
        }
        if (cam->start(&ctl))
            throw std::runtime_error("camera stream-on");
        bool queued = true;
        for (auto &r : reqs) {
            r->reuse(Request::ReuseBuffers);
            if (cam->queueRequest(r.get())) {
                queued = false;
                break;
            }
        }
        std::unique_lock<std::mutex> lock(mutex);
        bool ok = queued && cv.wait_for(lock, std::chrono::seconds(automatic ? 20 : 15 + seconds),
                                        [&] { return done; });
        done = true;
        lock.unlock();
        int stopResult = cam->stop();
        if (stopResult || !ok || !error.empty())
            throw std::runtime_error("capture stop=" + std::to_string(stopResult) + " frames=" +
                                     std::to_string(count) + " groups=" + std::to_string(groups) +
                                     (ok ? " " : " timeout ") + error);
    }

  private:
    uint64_t last = 0, start = 0, lastGroup = 0;
    uint32_t prev = 0;
    static constexpr int preamble = 68;
    static constexpr std::array<int, 31> pattern{0, 1, 0, 2, 2, 1, 1, 0, 2, 0, 1, 2, 1, 0, 0, 2,
                                                 1, 1, 2, 0, 2, 1, 0, 1, 2, 0, 1, 0, 2, 1, 0};
    std::array<Write, 8> writes{};
    std::array<double, preamble> light{};
    std::array<bool, preamble> early{};
    std::array<int64_t, preamble> writeLatencyNs{};
    using Group = std::array<Selected, 3>;
    Group group{};
    std::mutex pipeMutex;
    std::condition_variable pipeCV;
    std::optional<Group> waiting;
    bool closing = false;
    unsigned int writerDrops = 0;
    bool keepGroup = true;
    struct CaptureEvent {
        const char *event;
        uint32_t seq;
        int slot;
        uint64_t sensorTime, wallTime;
    };
    std::vector<CaptureEvent> events;
    void trace(const char *event) {
        if (!traceStartup && !traceTail) return;
        const uint64_t age = last - start;
        if ((traceStartup && age < 2000000000) ||
            (traceTail && age >= uint64_t(std::max(0, seconds - 62)) * 1000000000))
            events.push_back({event, prev, slots.slot, last, now()});
    }
    void selectGroup() {
        if (pending == 0) {
            if (slots.enabled()) {
                keepGroup = slots.acquire();
                trace(keepGroup ? "acquire" : "no_slot");
            }
            else {
                std::lock_guard<std::mutex> guard(pipeMutex);
                keepGroup = !waiting;
            }
        }
    }
    std::thread writer;
    bool sendGroup(const Group &frames, unsigned index) {
        for (const auto &f : frames) {
            const auto &w = f.command;
            std::cout << (slots.enabled() ? "HDR3 " : "HDR2 ") << index << " " << w.leg << " " << f.seq << " " << f.time << " "
                      << w.sequence << " " << w.end << " " << w.lines << " " << int(lineUs * w.lines)
                      << " " << f.delay << " " << w.frameTime;
            if (slots.enabled()) std::cout << " " << slots.slot;
            std::cout << "\n";
            if (!slots.enabled()) {
                for (const auto &plane : f.pixels)
                    std::cout.write(reinterpret_cast<const char *>(plane.data), plane.total() * plane.elemSize());
            }
        }
        return bool(std::cout.flush());
    }
    void sendGroups() {
        unsigned index = 0;
        for (;;) {
            std::unique_lock<std::mutex> guard(pipeMutex);
            pipeCV.wait(guard, [&] { return closing || waiting.has_value(); });
            if (!waiting)
                return;
            Group frames = std::move(*waiting);
            waiting.reset();
            guard.unlock();
            if (!sendGroup(frames, index)) return;
            ++index;
        }
    }
    void finishWriter() {
        if (!writer.joinable() && !slots.enabled()) return;
        if (writer.joinable()) {
            {
                std::lock_guard<std::mutex> guard(pipeMutex);
                closing = true;
            }
            pipeCV.notify_one();
            writer.join();
        }
        std::cerr << "capture groups=" << groups << " writer_dropped=" << writerDrops
                  << " late_frames=" << missedWrites << " optical_rejections=" << rejectedLight << "\n";
        for (const auto &e : events) {
            std::cerr << "HDR capture_event " << e.event << ' ' << e.seq << ' ' << e.slot
                      << ' ' << e.sensorTime << ' ' << e.wallTime << '\n';
        }
    }
    Write held{};
    int delay = 0, pending = 0;
    int expectedDelay() const { return rear && format.size != Size(4080, 2296) ? 3 : 2; }

    /* Fit brightness on the first part of the aperiodic sequence, classify a
     * disjoint suffix and check competing delays. Low contrast or motion can
     * invalidate calibration; keep the continuous stream and use held legs. */
    std::array<double, 3> centers{};
    unsigned missedWrites = 0, rejectedLight = 0;
    static int nearestLeg(const std::array<double, 3> &center, double value) {
        int nearest = 0;
        for (int j = 1; j < 3; ++j)
            if (std::abs(value - center[j]) < std::abs(value - center[nearest])) nearest = j;
        return nearest;
    }
    static double brightness(const cv::Mat &luma) {
        // Sample equal-area tiles in both the preamble and accepted frames.
        uint32_t sum = 0;
        for (int y = 0; y < 48; ++y) {
            const uint8_t *row = luma.ptr<uint8_t>((2*y + 1) * luma.rows / 96);
            for (int x = 0; x < 64; ++x) sum += row[(2*x + 1) * luma.cols / 128];
        }
        return double(sum) / (48 * 64);
    }
    int calibrate() {
        int best = 0, second = 0, winner = 0;
        const int expected = expectedDelay();
        const bool diagnostic = rear && format.size == Size(3840, 2160);
        std::ostringstream report;
        const char *expectedFailure = nullptr;
        for (int d = 1; d <= 5; d++) {
            std::array<double, 3> center{};
            std::array<double, 3> spread{};
            std::array<int, 3> samples{};
            bool enough = true;
            for (int leg = 0; leg < 3; leg++) {
                std::vector<double> values;
                for (int i = 9; i < 40; i++)
                    if (early[i - d] && pattern[(i - d) % 31] == leg)
                        values.push_back(light[i]);
                samples[leg] = values.size();
                if (values.size() < 5)
                    enough = false;
                if (values.empty())
                    continue;
                std::sort(values.begin(), values.end());
                center[leg] = values[values.size() / 2];
                for (double &v : values) v = std::abs(v - center[leg]);
                std::sort(values.begin(), values.end());
                spread[leg] = values[values.size() / 2];
            }
            int score = 0, timely = 0;
            // Separate the robust median bands; the independent suffix
            // and each selected live frame must also match its optical class.
            bool shortMiddle = center[1]-center[0] > 1 + 4*(spread[0]+spread[1]);
            bool middleLong = center[2]-center[1] > 1 + 4*(spread[1]+spread[2]);
            bool scored = enough && shortMiddle && middleLong;
            for (int i = 40; i < preamble; i++) {
                if (early[i-d]) {
                    ++timely;
                    if (scored && nearestLeg(center, light[i]) == pattern[(i-d) % 31])
                        ++score;
                }
            }
            if (diagnostic) {
                report << "HDR calibration delay=" << d << " training="
                       << samples[0] << ',' << samples[1] << ',' << samples[2]
                       << " median=" << center[0] << ',' << center[1] << ',' << center[2]
                       << " mad=" << spread[0] << ',' << spread[1] << ',' << spread[2]
                       << " separated=" << shortMiddle << ',' << middleLong
                       << " suffix_timely=" << timely << "/28 scored=" << scored
                       << " correct=" << score << "/28\n";
            }
            if (d == expected) {
                if (!enough) expectedFailure = "insufficient-timely-training";
                else if (!scored) expectedFailure = "overlapping-brightness-bands";
                else if (timely < 26) expectedFailure = "late-validation-writes";
                else if (score < 26) expectedFailure = "brightness-mismatch";
            }
            if (score > best) {
                second = best;
                best = score;
                winner = d;
                centers = center;
            } else
                second = std::max(second, score);
        }
        if (diagnostic) {
            const char *reason = expectedFailure ? expectedFailure : "accepted";
            if (best >= 26 && winner != expected) reason = "unexpected-delay";
            else if (best >= 26 && best-second < 5) reason = "ambiguous-delay";
            report << "HDR startup expected=" << expected << " window_ns=" << writeWindow
                   << " timely=" << std::count(early.begin(), early.end(), true) << '/' << preamble
                   << " gain=" << gain << " reason=" << reason << '\n';
            // Emit once after sampling. Each entry is the time from frame timestamp to completed write (ns)
            // and the measured luma sum over the 64x48 grid, in capture order.
            report << "HDR preamble first_seq=" << prev-(preamble-1) << " latency_ns/luma_sum=";
            for (int i = 0; i < preamble; ++i)
                report << (i ? "," : "") << writeLatencyNs[i] << '/' << std::lround(light[i]*3072);
            report << '\n';
            std::cerr << report.str();
        }
        std::cerr << "exposure lag=" << winner << " validation=" << best << "/28 next=" << second
                  << "\n";
        return best >= 26 && best - second >= 5 && winner == expectedDelay() ? winner : 0;
    }

    bool bracket(FrameBuffer *buf) {
        if (count == 1) {
            auto rate = get(V4L2_CID_PIXEL_RATE), blank = get(V4L2_CID_HBLANK);
            if (rate <= 0 || blank < 0)
                throw std::runtime_error("invalid line timing");
            lineUs = 1.e6 * (format.size.width + blank) / rate;
        }
        if (count <= preamble) {
            Write w = change(pattern[(count - 1) % 31]);
            early[count - 1] = w.timely(writeWindow);
            writeLatencyNs[count - 1] = int64_t(w.end) - int64_t(w.frameTime);
            light[count - 1] = sample(buf, true)[0].at<double>(0);
            if (count == preamble) {
                delay = calibrate();
                if (!slots.enabled()) writer = std::thread(&Source::sendGroups, this);
                std::cerr << (delay ? "interleaved exposures" : "held exposures") << "\n";
                if (!delay && requireInterleaved)
                    throw std::runtime_error("interleaved capture required: startup calibration rejected");
            }
            return false;
        }
        if (!start)
            start = last;
        if (groups && last - start >= uint64_t(seconds) * 1000000000)
            return true;
        /* A calibration can succeed with small metering buffers, while copying
         * full-size frames makes later exposure writes miss the deadline. */
        if (delay && last - (lastGroup ? lastGroup : start) > 500000000) {
            std::cerr << "interleaved capture stalled: groups=" << groups << " late_frames="
                      << missedWrites << " optical_rejections=" << rejectedLight << "\n";
            if (requireInterleaved)
                throw std::runtime_error("interleaved capture required: lost exposure pairing");
            std::cerr << "no verified interleaved group for 500 ms, switching to held exposures\n";
            delay = 0;
            pending = 0;
            group = {};
            held = {};
        }
        if (!delay) {
            if (!held.end) {
                held = change(0);
                return false;
            }
            if (prev - held.sequence < 4 || last < held.end + 3 * period)
                return false;
            if (get(V4L2_CID_EXPOSURE) != held.lines)
                throw std::runtime_error("exposure changed during held interval");
            selectGroup();
            if (keepGroup) group[pending] = {held, prev, last, sample(buf), delay};
            ++pending;
            held = change(pending % 3);
        } else {
            Write w = change((count - preamble - 1) % 3);
            writes[prev % writes.size()] = w;
            if (count <= preamble + delay + 1)
                return false;
            Write applied = writes[(prev - delay) % writes.size()];
            Write prior = writes[(prev - delay - 1) % writes.size()];
            if (applied.sequence + delay != prev || !applied.timely(writeWindow) || !prior.timely(writeWindow)) {
                trace("late");
                ++missedWrites;
                pending = 0;
                return false;
            }
            if (applied.leg == 0)
                pending = 0;
            if (applied.leg != pending) {
                pending = 0;
                return false;
            }
            selectGroup();
            Pixels images = sample(buf, !keepGroup);
            double light = keepGroup ? brightness(images[0]) : images[0].at<double>(0);
            if (nearestLeg(centers, light) != applied.leg) {
                trace("optical_reject");
                ++rejectedLight;
                pending = 0;
                return false;
            }
            if (keepGroup) group[pending] = {applied, prev, last, std::move(images), delay};
            ++pending;
        }
        if (pending < 3)
            return false;
        if (keepGroup && slots.enabled()) {
            if (!sendGroup(group, groups - writerDrops)) throw std::runtime_error("send shared bracket");
            trace("sent");
            slots.slot = -1;
        } else if (keepGroup) {
            {
                std::lock_guard<std::mutex> guard(pipeMutex);
                waiting = std::move(group);
            }
            pipeCV.notify_one();
        } else {
            ++writerDrops;
        }
        pending = 0;
        ++groups;
        lastGroup = last;
        return last - start >= uint64_t(seconds) * 1000000000;
    }
    int count = 0;
    bool warm = false, done = false;
    std::string error;
    std::mutex mutex;
    std::condition_variable cv;
    Pixels sample(FrameBuffer *b, bool meter = false) {
        const int w = format.size.width, h = format.size.height, s = format.stride;
        const size_t planes = b->planes().size();
        if ((planes != 1 && planes != 2) || b->metadata().planes().size() != planes)
            throw std::runtime_error("invalid NV12 plane count");
        int ow = output.width, oh = output.height;
        int cw = std::min(w, h * static_cast<int>(output.width) / static_cast<int>(output.height)) & ~1;
        int ch = std::min(h, cw * static_cast<int>(output.height) / static_cast<int>(output.width)) & ~1;
        int x = ((w - cw) / 2) & ~1, z = ((h - ch) / 2) & ~1;
        auto sample = [&](bool chroma) {
            unsigned idx = chroma && planes == 2 ? 1 : 0;
            auto &p = b->planes()[idx];
            size_t start = chroma && planes == 1 ? size_t(s) * h : 0;
            int div = chroma ? 2 : 1;
            size_t used = b->metadata().planes()[idx].bytesused;
            if (start + size_t(s) * h / div > used || used > p.length ||
                p.offset == FrameBuffer::Plane::kInvalidOffset)
                throw std::runtime_error("NV12 plane bounds");
            int fd = p.fd.get();
            dma_buf_sync sync{DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ};
            if (ioctl(fd, DMA_BUF_IOCTL_SYNC, &sync))
                throw std::runtime_error("start DMA read");
            size_t length = size_t(p.offset) + p.length;
            cv::Mat dst;
            try {
                auto m = std::find_if(mappings.begin(), mappings.end(), [&](const Mapping &m) {
                    return m.fd == fd && m.size == length;
                });
                if (m == mappings.end()) {
                    void *base = mmap(nullptr, length, PROT_READ, MAP_SHARED, fd, 0);
                    if (base == MAP_FAILED) throw std::runtime_error("map pixels");
                    mappings.push_back({fd, base, length});
                    m = std::prev(mappings.end());
                }
                void *addr = m->base;
                auto *data = static_cast<uint8_t *>(addr) + p.offset + start;
                data += (z / div) * s + x;
                if (meter) {
                    dst = cv::Mat(1, 1, CV_64F);
                    dst.at<double>(0) = brightness(cv::Mat(ch, cw, CV_8U, data, s));
                } else {
                    int type = chroma ? CV_8UC2 : CV_8U;
                    if (slots.enabled()) {
                        uint8_t *plane = slots.image(pending) + (chroma ? ow * oh : 0);
                        dst = cv::Mat(oh / div, ow / div, type, plane);
                    } else dst.create(oh / div, ow / div, type);
                    if (ow == cw && oh == ch && cw == s) {
                        std::memcpy(dst.data, data, size_t(ow) * oh / div);
                    } else {
                        int err = chroma ? libyuv::UVScale(data, s, cw / div, ch / div, dst.data, ow,
                                                           ow / div, oh / div, libyuv::kFilterBilinear)
                                         : libyuv::ScalePlane(data, s, cw, ch, dst.data, ow, ow, oh,
                                                              libyuv::kFilterBox);
                        if (err) throw std::runtime_error("NV12 scale");
                    }
                }
            } catch (...) {
                sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;
                ioctl(fd, DMA_BUF_IOCTL_SYNC, &sync);
                throw;
            }
            sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;
            if (ioctl(fd, DMA_BUF_IOCTL_SYNC, &sync))
                throw std::runtime_error("end DMA read");
            return dst;
        };
        return {sample(false), meter ? cv::Mat() : sample(true)};
    }
};

int main(int argc, char **argv) try {
    if (argc != 3 && argc != 4 && argc != 6)
        return 2;
    CameraManager manager;
    std::string name = argv[1];
    Source s;
    const char *required = std::getenv("LIUQIN_HDR_REQUIRE_INTERLEAVED");
    s.requireInterleaved = required && std::string(required) == "1";
    s.seconds = std::stoi(argv[2]);
    if ((name != "rear" && name != "front") || s.seconds < 1 || s.seconds > 3600)
        return 2;
    cv::setNumThreads(2);
    if (manager.start())
        throw std::runtime_error("libcamera");
    auto model = name == "rear" ? "s5kjn1" : "imx596";
    for (auto &c : manager.cameras())
        if (c->properties().get(properties::Model).value_or("") == model)
            s.cam = c;
    if (!s.cam || s.cam->acquire())
        throw std::runtime_error("camera acquire");
    for (auto &n : fs::directory_iterator("/sys/class/video4linux")) {
        std::string device;
        std::getline(std::ifstream(n.path() / "name"), device);
        if (device.rfind(std::string(model) + " ", 0) == 0 &&
            n.path().filename().string().rfind("v4l-subdev", 0) == 0) {
            s.sensor = open(("/dev/" + n.path().filename().string()).c_str(), O_RDWR | O_CLOEXEC);
            break;
        }
    }
    if (s.sensor < 0)
        throw std::runtime_error("sensor subdevice");
    auto cfg = s.cam->generateConfiguration({StreamRole::StillCapture});
    if (!cfg)
        throw std::runtime_error("configuration");
    auto &f = cfg->at(0);
    s.rear = name == "rear";
    Size desired = s.rear ? Size(4080, 3060) : Size(2592, 1952);
    if (argc >= 4) {
        std::string mode = argv[3];
        if (!s.rear) return 2;
        if (mode == "4080x2296") desired = Size(4080, 2296);
        else if (mode == "3840x2160") {
            desired = Size(3840, 2160);
            s.timesUs = {1000, 4000, 16000};
            s.period = 16667000;
            s.writeWindow = 6000000;
            const char *trace = std::getenv("LIUQIN_HDR_TRACE_STARTUP");
            s.traceStartup = trace && std::string(trace) == "1";
            trace = std::getenv("LIUQIN_HDR_TRACE_TAIL");
            s.traceTail = trace && std::string(trace) == "1";
        } else return 2;
        s.output = desired;
        if (argc == 6)
            s.slots.map(std::stoi(argv[4]), std::stoi(argv[5]), size_t(desired.width) * desired.height * 3 / 2);
    }
    f.size = desired;
    f.pixelFormat = formats::NV12;
    f.bufferCount = 4;
    if (cfg->validate() == CameraConfiguration::Invalid || f.size != desired ||
        f.pixelFormat != formats::NV12 || s.cam->configure(cfg.get()))
        throw std::runtime_error("capture mode");
    s.format = f;
    s.stream = f.stream();
    FrameBufferAllocator alloc(s.cam);
    if (alloc.allocate(s.stream) < 0)
        throw std::runtime_error("buffers");
    std::vector<std::unique_ptr<Request>> requests;
    for (auto &b : alloc.buffers(s.stream)) {
        auto r = s.cam->createRequest();
        if (!r || r->addBuffer(s.stream, b.get()))
            throw std::runtime_error("request");
        requests.push_back(std::move(r));
    }
    s.cam->requestCompleted.connect(&s, &Source::frame);
    ControlList ctl(s.cam->controls());
    s.run(requests, ctl, true);
    ctl.set(controls::AeEnable, false);
    ctl.set(controls::AwbEnable, false);
    ctl.set(controls::ExposureTime, s.timesUs[0]);
    ctl.set(controls::AnalogueGain, s.gain);
    ctl.set(controls::ColourGains, s.wb);
    if (s.haveCcm)
        ctl.set(controls::ColourCorrectionMatrix, s.ccm);
    if (s.cam->controls().count(&controls::AfMode)) {
        if (s.settledFocus && s.cam->controls().count(&controls::LensPosition)) {
            ctl.set(controls::AfMode, controls::AfModeManual);
            ctl.set(controls::LensPosition, *s.settledFocus);
        } else {
            ctl.set(controls::AfMode, controls::AfModeAuto);
            ctl.set(controls::AfTrigger, controls::AfTriggerCancel);
        }
    }
    ctl.set(controls::FrameDurationLimits, std::array<int64_t, 2>{int64_t(s.period / 1000), int64_t(s.period / 1000)});
    s.run(requests, ctl, false);
    s.cam->requestCompleted.disconnect(&s);
    requests.clear();
    alloc.free(s.stream);
    return 0;
} catch (const std::exception &e) {
    std::cerr << e.what() << "\n";
    return 1;
}
