// New integration code. See ../NOTICE.md for licensing status.
#include "ofxImageEffect.h"
#include "ofxProperty.h"
#include "frame_plan.hpp"
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <string>

namespace {
OfxHost* host = nullptr;
const OfxImageEffectSuiteV1* images = nullptr;
const OfxPropertySuiteV1* properties = nullptr;
const OfxParameterSuiteV1* parameters = nullptr;
std::mutex diagnostics_mutex;
unsigned diagnostics_count = 0;

struct ofx_error {
    OfxStatus status;
};

void require(OfxStatus status) {
    if (status != kOfxStatOK) throw ofx_error{status};
}

OfxPropertySetHandle effect_properties(OfxImageEffectHandle effect) {
    OfxPropertySetHandle result = nullptr;
    require(images->getPropertySet(effect, &result));
    return result;
}

struct instance {
    OfxImageClipHandle source = nullptr;
    OfxImageClipHandle output = nullptr;
    OfxParamHandle view = nullptr;
    OfxParamHandle offset = nullptr;
    OfxParamHandle diagnostics_path = nullptr;
};

instance& state(OfxImageEffectHandle effect) {
    void* data = nullptr;
    require(properties->propGetPointer(effect_properties(effect), kOfxPropInstanceData, 0, &data));
    if (!data) throw ofx_error{kOfxStatErrBadHandle};
    return *static_cast<instance*>(data);
}

struct settings {
    int view = 0;
    int offset = 1;
    std::string diagnostics_path;
};

settings read_settings(instance& data, double time) {
    settings result;
    char* path = nullptr;
    require(parameters->paramGetValueAtTime(data.view, time, &result.view));
    require(parameters->paramGetValueAtTime(data.offset, time, &result.offset));
    require(parameters->paramGetValue(data.diagnostics_path, &path));
    if (path) result.diagnostics_path = path;
    if (result.view < 0 || result.view > 1 || result.offset < 1 || result.offset > 20) {
        throw ofx_error{kOfxStatErrValue};
    }
    return result;
}

std::array<double, 3> planned_frames(instance& data, double time, int offset) {
    OfxPropertySetHandle clip = nullptr;
    require(images->clipGetPropertySet(data.source, &clip));
    double range[2] = {};
    require(properties->propGetDoubleN(clip, kOfxImageEffectPropFrameRange, 2, range));
    return deflicker::frame_plan(time, offset, range[0], range[1]);
}

class image {
public:
    OfxPropertySetHandle handle = nullptr;
    std::byte* data = nullptr;
    int bounds[4] = {};
    int stride = 0;
    int channels = 0;
    double actual_time = std::numeric_limits<double>::quiet_NaN();

    image(OfxImageClipHandle clip, double time) {
        require(images->clipGetImage(clip, time, nullptr, &handle));
        try {
            void* pixels = nullptr;
            char* depth = nullptr;
            char* components = nullptr;
            require(properties->propGetPointer(handle, kOfxImagePropData, 0, &pixels));
            require(properties->propGetIntN(handle, kOfxImagePropBounds, 4, bounds));
            require(properties->propGetInt(handle, kOfxImagePropRowBytes, 0, &stride));
            require(properties->propGetString(handle, kOfxImageEffectPropPixelDepth, 0, &depth));
            require(properties->propGetString(handle, kOfxImageEffectPropComponents, 0, &components));
            if (!pixels || !depth || !components || std::strcmp(depth, kOfxBitDepthFloat)) {
                throw ofx_error{kOfxStatErrImageFormat};
            }
            if (!std::strcmp(components, kOfxImageComponentRGBA)) channels = 4;
            if (!std::strcmp(components, kOfxImageComponentRGB)) channels = 3;
            if (!channels || bounds[2] <= bounds[0] || bounds[3] <= bounds[1]) {
                throw ofx_error{kOfxStatErrImageFormat};
            }
            data = static_cast<std::byte*>(pixels);
            properties->propGetDouble(handle, kOfxPropTime, 0, &actual_time);
        } catch (...) {
            images->clipReleaseImage(handle);
            handle = nullptr;
            throw;
        }
    }

    image(const image&) = delete;
    image& operator=(const image&) = delete;
    ~image() { if (handle) images->clipReleaseImage(handle); }

    std::byte* pixel(int x, int y) const {
        if (x < bounds[0] || x >= bounds[2] || y < bounds[1] || y >= bounds[3]) {
            throw ofx_error{kOfxStatErrImageFormat};
        }
        return data + static_cast<std::ptrdiff_t>(y - bounds[1]) * stride +
               static_cast<std::ptrdiff_t>(x - bounds[0]) * channels * sizeof(float);
    }

    std::uint64_t sample_hash() const {
        std::uint64_t hash = 14695981039346656037ULL;
        // Fixed sample grid keeps diagnostics bounded at UHD and above.
        for (int gy = 0; gy < 8; ++gy) {
            for (int gx = 0; gx < 8; ++gx) {
                int x = bounds[0] + (bounds[2] - bounds[0] - 1) * gx / 7;
                int y = bounds[1] + (bounds[3] - bounds[1] - 1) * gy / 7;
                const auto* bytes = reinterpret_cast<const unsigned char*>(pixel(x, y));
                for (std::size_t index = 0; index < channels * sizeof(float); ++index) {
                    hash = (hash ^ bytes[index]) * 1099511628211ULL;
                }
            }
        }
        return hash;
    }
};

OfxStatus describe(OfxImageEffectHandle effect) {
    int temporal = 0;
    properties->propGetInt(host->host, kOfxImageEffectPropTemporalClipAccess, 0, &temporal);
    if (!temporal) return kOfxStatErrMissingHostFeature;
    const auto descriptor = effect_properties(effect);
    require(properties->propSetString(descriptor, kOfxPropLabel, 0, "Image-Based Deflicker Frame Probe"));
    require(properties->propSetString(descriptor, kOfxImageEffectPluginPropGrouping, 0, "Image-Based Deflicker"));
    require(properties->propSetString(descriptor, kOfxImageEffectPropSupportedContexts, 0, kOfxImageEffectContextFilter));
    require(properties->propSetString(descriptor, kOfxImageEffectPropSupportedContexts, 1, kOfxImageEffectContextGeneral));
    require(properties->propSetString(descriptor, kOfxImageEffectPropSupportedPixelDepths, 0, kOfxBitDepthFloat));
    require(properties->propSetInt(descriptor, kOfxImageEffectPropTemporalClipAccess, 0, 1));
    require(properties->propSetInt(descriptor, kOfxImageEffectPropSupportsTiles, 0, 0));
    require(properties->propSetInt(descriptor, kOfxImageEffectPropSupportsMultiResolution, 0, 1));
    require(properties->propSetInt(descriptor, kOfxImageEffectPluginPropHostFrameThreading, 0, 0));
    require(properties->propSetString(descriptor, kOfxImageEffectPluginRenderThreadSafety, 0, kOfxImageEffectRenderFullySafe));
    return kOfxStatOK;
}

OfxStatus describe_context(OfxImageEffectHandle effect) {
    for (const auto* name : {kOfxImageEffectSimpleSourceClipName, kOfxImageEffectOutputClipName}) {
        OfxPropertySetHandle clip = nullptr;
        require(images->clipDefine(effect, name, &clip));
        require(properties->propSetString(clip, kOfxImageEffectPropSupportedComponents, 0, kOfxImageComponentRGBA));
        require(properties->propSetString(clip, kOfxImageEffectPropSupportedComponents, 1, kOfxImageComponentRGB));
        require(properties->propSetInt(clip, kOfxImageEffectPropSupportsTiles, 0, 0));
        if (!std::strcmp(name, kOfxImageEffectSimpleSourceClipName)) {
            require(properties->propSetInt(clip, kOfxImageEffectPropTemporalClipAccess, 0, 1));
        }
    }
    OfxParamSetHandle set = nullptr;
    require(images->getParamSet(effect, &set));
    OfxPropertySetHandle parameter = nullptr;
    require(parameters->paramDefine(set, kOfxParamTypeChoice, "view", &parameter));
    require(properties->propSetString(parameter, kOfxPropLabel, 0, "View"));
    require(properties->propSetString(parameter, kOfxParamPropChoiceOption, 0, "Previous / Current / Next"));
    require(properties->propSetString(parameter, kOfxParamPropChoiceOption, 1, "Original"));
    require(properties->propSetInt(parameter, kOfxParamPropDefault, 0, 0));
    require(properties->propSetInt(parameter, kOfxParamPropAnimates, 0, 0));
    require(parameters->paramDefine(set, kOfxParamTypeInteger, "frameOffset", &parameter));
    require(properties->propSetString(parameter, kOfxPropLabel, 0, "Frame offset"));
    require(properties->propSetInt(parameter, kOfxParamPropDefault, 0, 1));
    require(properties->propSetInt(parameter, kOfxParamPropMin, 0, 1));
    require(properties->propSetInt(parameter, kOfxParamPropMax, 0, 20));
    require(properties->propSetInt(parameter, kOfxParamPropDisplayMin, 0, 1));
    require(properties->propSetInt(parameter, kOfxParamPropDisplayMax, 0, 5));
    require(properties->propSetInt(parameter, kOfxParamPropAnimates, 0, 0));
    require(parameters->paramDefine(set, kOfxParamTypeString, "diagnosticsPath", &parameter));
    require(properties->propSetString(parameter, kOfxPropLabel, 0, "Optional diagnostic log"));
    require(properties->propSetString(parameter, kOfxParamPropStringMode, 0, kOfxParamStringIsFilePath));
    require(properties->propSetString(parameter, kOfxParamPropDefault, 0, ""));
    require(properties->propSetInt(parameter, kOfxParamPropAnimates, 0, 0));
    return kOfxStatOK;
}

OfxStatus create_instance(OfxImageEffectHandle effect) {
    auto data = std::make_unique<instance>();
    require(images->clipGetHandle(effect, kOfxImageEffectSimpleSourceClipName, &data->source, nullptr));
    require(images->clipGetHandle(effect, kOfxImageEffectOutputClipName, &data->output, nullptr));
    OfxParamSetHandle set = nullptr;
    require(images->getParamSet(effect, &set));
    require(parameters->paramGetHandle(set, "view", &data->view, nullptr));
    require(parameters->paramGetHandle(set, "frameOffset", &data->offset, nullptr));
    require(parameters->paramGetHandle(set, "diagnosticsPath", &data->diagnostics_path, nullptr));
    require(properties->propSetPointer(effect_properties(effect), kOfxPropInstanceData, 0, data.get()));
    data.release();
    return kOfxStatOK;
}

OfxStatus frames_needed(OfxImageEffectHandle effect, OfxPropertySetHandle in, OfxPropertySetHandle out) {
    double time = 0;
    require(properties->propGetDouble(in, kOfxPropTime, 0, &time));
    auto& data = state(effect);
    const auto values = read_settings(data, time);
    auto times = planned_frames(data, time, values.offset);
    if (values.view == 1) times.fill(time);
    const double range[] = {times.front(), times.back()};
    require(properties->propSetDoubleN(out, "OfxImageClipPropFrameRange_Source", 2, range));
    return kOfxStatOK;
}

void log_diagnostics(const settings& values, double time, const std::array<double, 3>& times,
                     const std::array<image*, 3>& frames, const image& output, double milliseconds) {
    if (values.diagnostics_path.empty()) return;
    std::lock_guard<std::mutex> lock(diagnostics_mutex);
    if (diagnostics_count >= 2000) return;
    std::ofstream log(values.diagnostics_path, std::ios::app);
    if (!log) return;
    ++diagnostics_count;
    log << std::setprecision(12) << "{\"time\":" << time << ",\"view\":" << values.view
        << ",\"width\":" << output.bounds[2] - output.bounds[0]
        << ",\"height\":" << output.bounds[3] - output.bounds[1]
        << ",\"requested\":[" << times[0] << ',' << times[1] << ',' << times[2] << "]"
        << ",\"actual\":[";
    for (int index = 0; index < 3; ++index) {
        if (index) log << ',';
        if (std::isfinite(frames[index]->actual_time)) log << frames[index]->actual_time;
        else log << "null";
    }
    log << "],\"hashes\":[";
    for (int index = 0; index < 3; ++index) {
        if (index) log << ',';
        log << '"' << frames[index]->sample_hash() << '"';
    }
    log << "],\"processing_ms\":" << milliseconds << "}\n";
}

OfxStatus render(OfxImageEffectHandle effect, OfxPropertySetHandle in) {
    const auto started = std::chrono::steady_clock::now();
    auto& data = state(effect);
    double time = 0;
    int window[4] = {};
    require(properties->propGetDouble(in, kOfxPropTime, 0, &time));
    require(properties->propGetIntN(in, kOfxImageEffectPropRenderWindow, 4, window));
    const auto values = read_settings(data, time);
    if (images->abort(effect)) return kOfxStatOK;
    image current(data.source, time);
    image output(data.output, time);
    std::unique_ptr<image> previous;
    std::unique_ptr<image> next;
    auto times = std::array<double, 3>{time, time, time};
    std::array<image*, 3> frames = {&current, &current, &current};
    if (values.view == 0) {
        times = planned_frames(data, time, values.offset);
        if (times[0] != time) previous = std::make_unique<image>(data.source, times[0]);
        if (times[2] != time) next = std::make_unique<image>(data.source, times[2]);
        if (previous) frames[0] = previous.get();
        if (next) frames[2] = next.get();
    }
    for (const auto* frame : frames) {
        if (frame->channels != output.channels) throw ofx_error{kOfxStatErrImageFormat};
    }
    const int width = output.bounds[2] - output.bounds[0];
    const int height = output.bounds[3] - output.bounds[1];
    for (int y = std::max(window[1], output.bounds[1]); y < std::min(window[3], output.bounds[3]); ++y) {
        if (images->abort(effect)) return kOfxStatOK;
        for (int x = std::max(window[0], output.bounds[0]); x < std::min(window[2], output.bounds[2]); ++x) {
            int column = 1;
            int source_x = x;
            int source_y = y;
            if (values.view == 0) {
                const auto position = static_cast<std::int64_t>(x - output.bounds[0]) * 3;
                column = std::min(2, static_cast<int>(position / width));
                const auto* frame = frames[column];
                source_x = frame->bounds[0] + static_cast<int>((position % width) *
                           (frame->bounds[2] - frame->bounds[0]) / width);
                source_y = frame->bounds[1] + static_cast<int>(static_cast<std::int64_t>(y - output.bounds[1]) *
                           (frame->bounds[3] - frame->bounds[1]) / height);
            }
            std::memcpy(output.pixel(x, y), frames[column]->pixel(source_x, source_y),
                        output.channels * sizeof(float));
        }
    }
    const auto elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
    log_diagnostics(values, time, times, frames, output, elapsed);
    return kOfxStatOK;
}

OfxStatus entry(const char* action, const void* handle, OfxPropertySetHandle in, OfxPropertySetHandle out) {
    try {
        auto effect = reinterpret_cast<OfxImageEffectHandle>(const_cast<void*>(handle));
        if (!std::strcmp(action, kOfxActionLoad)) {
            images = static_cast<const OfxImageEffectSuiteV1*>(host->fetchSuite(host->host, kOfxImageEffectSuite, 1));
            properties = static_cast<const OfxPropertySuiteV1*>(host->fetchSuite(host->host, kOfxPropertySuite, 1));
            parameters = static_cast<const OfxParameterSuiteV1*>(host->fetchSuite(host->host, kOfxParameterSuite, 1));
            return images && properties && parameters ? kOfxStatOK : kOfxStatErrMissingHostFeature;
        }
        if (!std::strcmp(action, kOfxActionDescribe)) return describe(effect);
        if (!std::strcmp(action, kOfxImageEffectActionDescribeInContext)) return describe_context(effect);
        if (!std::strcmp(action, kOfxActionCreateInstance)) return create_instance(effect);
        if (!std::strcmp(action, kOfxActionDestroyInstance)) {
            std::unique_ptr<instance> owner(&state(effect));
            require(properties->propSetPointer(effect_properties(effect), kOfxPropInstanceData, 0, nullptr));
            return kOfxStatOK;
        }
        if (!std::strcmp(action, kOfxImageEffectActionRender)) return render(effect, in);
        if (!std::strcmp(action, kOfxImageEffectActionGetFramesNeeded)) return frames_needed(effect, in, out);
        if (!std::strcmp(action, kOfxImageEffectActionGetClipPreferences)) {
            return properties->propSetInt(out, kOfxImageEffectFrameVarying, 0, 1);
        }
        if (!std::strcmp(action, kOfxImageEffectActionGetRegionOfDefinition) ||
            !std::strcmp(action, kOfxImageEffectActionGetRegionsOfInterest)) {
            double time = 0;
            require(properties->propGetDouble(in, kOfxPropTime, 0, &time));
            OfxRectD rectangle = {};
            require(images->clipGetRegionOfDefinition(state(effect).source, time, &rectangle));
            const double values[] = {rectangle.x1, rectangle.y1, rectangle.x2, rectangle.y2};
            const auto* key = !std::strcmp(action, kOfxImageEffectActionGetRegionOfDefinition)
                            ? kOfxImageEffectPropRegionOfDefinition : "OfxImageClipPropRoI_Source";
            return properties->propSetDoubleN(out, key, 4, values);
        }
        return kOfxStatReplyDefault;
    } catch (const ofx_error& error) {
        return error.status;
    } catch (const std::bad_alloc&) {
        return kOfxStatErrMemory;
    } catch (...) {
        return kOfxStatFailed;
    }
}

void set_host(OfxHost* value) { host = value; }
OfxPlugin plugin = {kOfxImageEffectPluginApi, 1, "org.imagebaseddeflicker.frameprobe", 0, 1, set_host, entry};
}

extern "C" __attribute__((visibility("default"))) int OfxGetNumberOfPlugins() { return 1; }
extern "C" __attribute__((visibility("default"))) OfxPlugin* OfxGetPlugin(int index) {
    return index == 0 ? &plugin : nullptr;
}
