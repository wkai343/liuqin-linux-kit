// SPDX-License-Identifier: MIT
// On-device optical bench. Scan DACs with single-shot AF idle, or send standard
// LensPosition requests and observe their settled frame metadata and readback.
#include <libcamera/libcamera.h>
#include <linux/dma-buf.h>
#include <linux/videodev2.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <vector>

using namespace libcamera;
using namespace std::chrono_literals;

static void require(bool ok, const char *what)
{
    if (!ok)
        throw std::runtime_error(what);
}

class Completed {
public:
    void push(Request *r)
    {
        std::lock_guard lock(mutex_);
        queue_.push_back(r);
        wake_.notify_one();
    }

    Request *next()
    {
        std::unique_lock lock(mutex_);
        require(wake_.wait_for(lock, 5s, [&] { return !queue_.empty(); }), "frame timeout");
        Request *r = queue_.front();
        queue_.pop_front();
        require(r->status() == Request::RequestComplete, "request cancelled");
        return r;
    }

private:
    std::mutex mutex_;
    std::condition_variable wake_;
    std::deque<Request *> queue_;
};

static double sharpness(const FrameBuffer *buffer, const StreamConfiguration &s,
                        const std::string &framePath)
{
    require(!buffer->planes().empty(), "missing Y plane");
    const auto &p = buffer->planes()[0];
    require(p.length >= size_t(s.stride) * s.size.height, "short Y plane");
    size_t length = size_t(p.offset) + p.length;
    void *map = mmap(nullptr, length, PROT_READ, MAP_SHARED, p.fd.get(), 0);
    require(map != MAP_FAILED, "mmap");
    dma_buf_sync sync{ DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ };
    require(ioctl(p.fd.get(), DMA_BUF_IOCTL_SYNC, &sync) == 0, "begin DMA read");
    auto *y = static_cast<const uint8_t *>(map) + p.offset;
    const int w = s.size.width, h = s.size.height;
    uint64_t sum = 0, energy = 0, count = 0;
    for (int row = h * 3 / 8; row < h * 5 / 8; row += 3) {
        for (int col = w * 3 / 8; col < w * 5 / 8; col += 3) {
            const auto *v = y + row * s.stride + col;
            int dx = v[2] - v[-2], dy = v[2 * int(s.stride)] - v[-2 * int(s.stride)];
            energy += dx * dx + dy * dy;
            sum += *v;
            ++count;
        }
    }
    if (!framePath.empty()) {
        std::ofstream image(framePath, std::ios::binary);
        image << "P5\n" << w << ' ' << h << "\n255\n";
        for (int j = 0; j < h; ++j)
            image.write(reinterpret_cast<const char *>(y + j * s.stride), w);
        require(bool(image), "save focus verification frame");
    }
    sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;
    require(ioctl(p.fd.get(), DMA_BUF_IOCTL_SYNC, &sync) == 0, "end DMA read");
    munmap(map, length);
    require(sum > count * 8, "target too dark");
    return double(energy) * count / (double(sum) * sum);
}

int main(int argc, char **argv)
try {
    require(argc >= 5, "usage: camera-rear-manual-focus scan|manual LENS_SUBDEV SAMPLES POSITIONS...");
    const bool manual = std::string(argv[1]) == "manual";
    require(manual || std::string(argv[1]) == "scan", "invalid mode");
    const int samples = std::stoi(argv[3]);
    require(samples > 0 && samples <= 300, "invalid sample count");
    int lens = open(argv[2], (manual ? O_RDONLY : O_RDWR) | O_CLOEXEC);
    require(lens >= 0, "open lens");
    auto lensDac = [&] {
        v4l2_control c{V4L2_CID_FOCUS_ABSOLUTE, 0};
        require(ioctl(lens, VIDIOC_G_CTRL, &c) == 0, "lens readback");
        return c.value;
    };
    CameraManager manager;
    require(manager.start() == 0, "camera manager");
    auto camera = manager.get("/base/soc@0/cci@ac15000/i2c-bus@1/camera@10");
    require(camera && camera->acquire() == 0, "acquire rear camera");
    auto config = camera->generateConfiguration({StreamRole::StillCapture});
    auto &s = config->at(0);
    s.size = {4080, 3060};
    s.pixelFormat = formats::NV12;
    require(config->validate() != CameraConfiguration::Invalid && s.size == Size(4080, 3060)
            && s.pixelFormat == formats::NV12 && camera->configure(config.get()) == 0, "rear ISP configuration");
    const auto positions = camera->controls().find(&controls::LensPosition);
    if (manual)
        require(positions != camera->controls().end(), "no calibrated LensPosition control");
    FrameBufferAllocator allocator(camera);
    require(allocator.allocate(s.stream()) >= 0, "buffer allocation");
    std::vector<std::unique_ptr<Request>> requests;
    for (const auto &b : allocator.buffers(s.stream())) {
        auto r = camera->createRequest();
        require(r && r->addBuffer(s.stream(), b.get()) == 0, "add buffer");
        requests.push_back(std::move(r));
    }
    Completed completed;
    camera->requestCompleted.connect(&completed, &Completed::push);
    ControlList initial(camera->controls());
    initial.set(controls::AfMode, manual ? controls::AfModeManual : controls::AfModeAuto);
    if (manual)
        initial.set(controls::LensPosition, std::stof(argv[4]));
    require(camera->start(&initial) == 0, "start camera");
    try {
        for (const auto &r : requests)
            require(camera->queueRequest(r.get()) == 0, "initial request");
        auto recycle = [&](Request *r) {
            r->reuse(Request::ReuseBuffers);
            require(camera->queueRequest(r) == 0, "queue empty request");
        };
        for (int i = 0; i < 25; ++i)
            recycle(completed.next());
        std::cout << "stage,target,sequence,timestamp_ns,dac,position,mode,state,contrast\n";
        for (int a = 4; a < argc; ++a) {
            float target = std::stof(argv[a]);
            require(std::isfinite(target), "invalid position");
            if (manual) {
                require(target >= positions->second.min().get<float>() && target <= positions->second.max().get<float>(), "outside calibrated interval");
                auto *r = completed.next();
                r->reuse(Request::ReuseBuffers);
                r->controls().set(controls::LensPosition, target);
                require(camera->queueRequest(r) == 0, "send lens request");
            } else {
                require(target >= 0 && target <= 1023 && std::floor(target) == target, "invalid DAC");
                v4l2_control c{V4L2_CID_FOCUS_ABSOLUTE, int(target)};
                require(ioctl(lens, VIDIOC_S_CTRL, &c) == 0, "move calibration lens");
            }
            // Drop queued and in-flight frames. Require 200 ms of stream time
            // beyond the last outstanding request before sampling optical sharpness.
            for (size_t i = 0; i <= requests.size(); ++i) recycle(completed.next());
            uint64_t first = 0;
            std::optional<int> settledDac;
            for (int n = 0; n < samples;) {
                auto *r = completed.next();
                auto *b = r->findBuffer(s.stream());
                require(b && b->metadata().status == FrameMetadata::FrameSuccess, "bad output");
                auto ts = b->metadata().timestamp;
                if (!first) first = ts;
                if (ts > first + 200000000ULL) {
                    auto pos = r->metadata().get(controls::LensPosition);
                    auto mode = r->metadata().get(controls::AfMode), state = r->metadata().get(controls::AfState);
                    int dac = lensDac();
                    require(manual ? (mode == controls::AfModeManual && state == controls::AfStateIdle && pos &&
                                      std::abs(*pos - target) < 0.08f) : (mode == controls::AfModeAuto && dac == int(target)), "settled focus mismatch");
                    if (settledDac)
                        require(dac == *settledDac, "lens setting changed during hold");
                    settledDac = dac;
                    auto framePath = manual && n == 0 ? "focus-stage-" + std::to_string(a-4) + ".pgm" : "";
                    std::cout << a-4 << ',' << target << ',' << b->metadata().sequence << ',' << ts << ',' << dac << ','
                              << pos.value_or(-1) << ',' << mode.value_or(-1) << ',' << state.value_or(-1) << ','
                              << std::setprecision(9) << sharpness(b, s, framePath) << '\n';
                    ++n;
                }
                recycle(r);
            }
        }
    } catch (...) {
        camera->stop();
        camera->requestCompleted.disconnect(&completed);
        throw;
    }
    camera->stop();
    camera->requestCompleted.disconnect(&completed);
    requests.clear(); allocator.free(s.stream()); camera->release(); close(lens);
    return 0;
} catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
}
