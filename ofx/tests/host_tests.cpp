// New integration code. See ../NOTICE.md for licensing status.
// A small independent host exercises the plug-in's actual exported entry point.
#include <cassert>
#include <cstdarg>
#include <filesystem>
#include <iostream>
#include <map>
#include <variant>
#include <vector>
#include "../src/plugin.cpp"

namespace test_host {
using value = std::variant<void*, int, double, std::string>;
using property_set = std::map<std::string, std::vector<value>>;
property_set descriptor, host_properties, source_properties, output_properties, in_args, out_args;
std::map<std::string, property_set> parameter_properties, clip_properties;
struct parameter { std::string name; };
std::map<std::string, parameter> parameter_handles;
struct fixture {
    property_set properties;
    std::vector<float> pixels;
};
std::map<int, fixture> source_frames;
fixture output_frame;
int view = 0;
int offset = 1;
bool aborted = false;
int outstanding = 0;
int missing_time = -1;
std::string diagnostics_path;
std::vector<double> fetched;
constexpr int width = 12;
constexpr int height = 4;

OfxPropertySetHandle handle(property_set& set) { return reinterpret_cast<OfxPropertySetHandle>(&set); }

template<class T> OfxStatus put(OfxPropertySetHandle set, const char* key, int index, T data) {
    if (index < 0) return kOfxStatErrBadIndex;
    auto& values = (*reinterpret_cast<property_set*>(set))[key];
    if (values.size() <= static_cast<std::size_t>(index)) values.resize(index + 1);
    values[index] = data;
    return kOfxStatOK;
}

template<class T> OfxStatus get(OfxPropertySetHandle set, const char* key, int index, T* data) {
    const auto& values = *reinterpret_cast<property_set*>(set);
    const auto found = values.find(key);
    if (found == values.end()) return kOfxStatErrUnknown;
    if (index < 0 || static_cast<std::size_t>(index) >= found->second.size()) return kOfxStatErrBadIndex;
    const auto* item = std::get_if<T>(&found->second[index]);
    if (!item) return kOfxStatErrValue;
    *data = *item;
    return kOfxStatOK;
}

OfxStatus put_string(OfxPropertySetHandle set, const char* key, int index, const char* data) {
    return put(set, key, index, std::string(data));
}
OfxStatus get_string(OfxPropertySetHandle set, const char* key, int index, char** data) {
    auto& values = *reinterpret_cast<property_set*>(set);
    const auto found = values.find(key);
    if (found == values.end()) return kOfxStatErrUnknown;
    *data = std::get<std::string>(found->second.at(index)).data();
    return kOfxStatOK;
}
template<class T> OfxStatus get_many(OfxPropertySetHandle set, const char* key, int count, T* data) {
    for (int index = 0; index < count; ++index) {
        const auto status = get(set, key, index, data + index);
        if (status != kOfxStatOK) return status;
    }
    return kOfxStatOK;
}
OfxStatus put_doubles(OfxPropertySetHandle set, const char* key, int count, const double* data) {
    for (int index = 0; index < count; ++index) put(set, key, index, data[index]);
    return kOfxStatOK;
}
OfxStatus effect_property_set(OfxImageEffectHandle, OfxPropertySetHandle* set) {
    *set = handle(descriptor); return kOfxStatOK;
}
OfxStatus parameter_set(OfxImageEffectHandle, OfxParamSetHandle* set) {
    *set = reinterpret_cast<OfxParamSetHandle>(&parameter_handles); return kOfxStatOK;
}
OfxStatus define_clip(OfxImageEffectHandle, const char* name, OfxPropertySetHandle* set) {
    *set = handle(clip_properties[name]); return kOfxStatOK;
}
OfxStatus clip_handle(OfxImageEffectHandle, const char* name, OfxImageClipHandle* clip, OfxPropertySetHandle* set) {
    auto& properties = !std::strcmp(name, "Source") ? source_properties : output_properties;
    *clip = reinterpret_cast<OfxImageClipHandle>(&properties);
    if (set) *set = handle(properties);
    return kOfxStatOK;
}
OfxStatus clip_property_set(OfxImageClipHandle clip, OfxPropertySetHandle* set) {
    *set = reinterpret_cast<OfxPropertySetHandle>(clip); return kOfxStatOK;
}
OfxStatus fetch_image(OfxImageClipHandle clip, double time, const OfxRectD*, OfxPropertySetHandle* set) {
    if (clip == reinterpret_cast<OfxImageClipHandle>(&output_properties)) {
        *set = handle(output_frame.properties);
    } else {
        fetched.push_back(time);
        if (time == missing_time || !source_frames.contains(static_cast<int>(time))) return kOfxStatFailed;
        *set = handle(source_frames.at(static_cast<int>(time)).properties);
    }
    ++outstanding;
    return kOfxStatOK;
}
OfxStatus release_image(OfxPropertySetHandle) { --outstanding; return kOfxStatOK; }
OfxStatus region(OfxImageClipHandle, double, OfxRectD* result) {
    *result = {10, 20, 10 + width, 20 + height}; return kOfxStatOK;
}
OfxStatus define_parameter(OfxParamSetHandle, const char*, const char* name, OfxPropertySetHandle* set) {
    parameter_handles[name] = {name};
    *set = handle(parameter_properties[name]); return kOfxStatOK;
}
OfxStatus parameter_handle(OfxParamSetHandle, const char* name, OfxParamHandle* result, OfxPropertySetHandle*) {
    *result = reinterpret_cast<OfxParamHandle>(&parameter_handles.at(name)); return kOfxStatOK;
}
OfxStatus parameter_value_at_time(OfxParamHandle parameter, double time, ...) {
    va_list arguments;
    va_start(arguments, time);
    const auto& name = reinterpret_cast<test_host::parameter*>(parameter)->name;
    *va_arg(arguments, int*) = name == "view" ? view : offset;
    va_end(arguments);
    return kOfxStatOK;
}
OfxStatus parameter_value(OfxParamHandle parameter, ...) {
    va_list arguments;
    va_start(arguments, parameter);
    *va_arg(arguments, char**) = diagnostics_path.data();
    va_end(arguments);
    return kOfxStatOK;
}
OfxPropertySuiteV1 property_suite = {};
OfxImageEffectSuiteV1 image_suite = {};
OfxParameterSuiteV1 parameter_suite = {};
const void* fetch_suite(OfxPropertySetHandle, const char* name, int) {
    if (!std::strcmp(name, kOfxPropertySuite)) return &property_suite;
    if (!std::strcmp(name, kOfxImageEffectSuite)) return &image_suite;
    if (!std::strcmp(name, kOfxParameterSuite)) return &parameter_suite;
    return nullptr;
}

void configure_fixture(fixture& frame, int time, int channels, bool reverse) {
    frame.pixels.resize(width * height * channels);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            for (int channel = 0; channel < channels; ++channel) {
                frame.pixels[(y * width + x) * channels + channel] =
                    static_cast<float>(time + x * 0.01 + y * 0.001 + channel * 0.0001);
            }
        }
    }
    const auto set = handle(frame.properties);
    float* first = frame.pixels.data();
    int stride = width * channels * sizeof(float);
    if (reverse) { first += width * channels * (height - 1); stride = -stride; }
    put(set, kOfxImagePropData, 0, static_cast<void*>(first));
    put(set, kOfxImagePropRowBytes, 0, stride);
    const int bounds[] = {10, 20, 10 + width, 20 + height};
    for (int index = 0; index < 4; ++index) put(set, kOfxImagePropBounds, index, bounds[index]);
    put(set, kOfxPropTime, 0, static_cast<double>(time));
    put_string(set, kOfxImageEffectPropPixelDepth, 0, kOfxBitDepthFloat);
    put_string(set, kOfxImageEffectPropComponents, 0,
               channels == 4 ? kOfxImageComponentRGBA : kOfxImageComponentRGB);
}

void prepare_frames(int channels = 4, bool reverse = false) {
    for (int time = 10; time <= 14; ++time) configure_fixture(source_frames[time], time, channels, reverse);
    configure_fixture(output_frame, 0, channels, false);
    const int window[] = {10, 20, 10 + width, 20 + height};
    for (int index = 0; index < 4; ++index) put(handle(in_args), kOfxImageEffectPropRenderWindow, index, window[index]);
}
}

int main() {
    using namespace test_host;
    property_suite.propSetPointer = put<void*>;
    property_suite.propSetInt = put<int>;
    property_suite.propSetDouble = put<double>;
    property_suite.propSetString = put_string;
    property_suite.propGetPointer = get<void*>;
    property_suite.propGetInt = get<int>;
    property_suite.propGetDouble = get<double>;
    property_suite.propGetString = get_string;
    property_suite.propGetIntN = get_many<int>;
    property_suite.propGetDoubleN = get_many<double>;
    property_suite.propSetDoubleN = put_doubles;
    image_suite.getPropertySet = effect_property_set;
    image_suite.getParamSet = parameter_set;
    image_suite.clipDefine = define_clip;
    image_suite.clipGetHandle = clip_handle;
    image_suite.clipGetPropertySet = clip_property_set;
    image_suite.clipGetImage = fetch_image;
    image_suite.clipReleaseImage = release_image;
    image_suite.clipGetRegionOfDefinition = region;
    image_suite.abort = [](OfxImageEffectHandle) { return aborted ? 1 : 0; };
    parameter_suite.paramDefine = define_parameter;
    parameter_suite.paramGetHandle = parameter_handle;
    parameter_suite.paramGetValueAtTime = parameter_value_at_time;
    parameter_suite.paramGetValue = parameter_value;
    OfxHost test_host = {handle(host_properties), fetch_suite};
    auto* exported = OfxGetPlugin(0);
    assert(OfxGetNumberOfPlugins() == 1 && OfxGetPlugin(1) == nullptr && OfxGetPlugin(-1) == nullptr);
    exported->setHost(&test_host);
    const auto effect = reinterpret_cast<OfxImageEffectHandle>(&descriptor);
    assert(exported->mainEntry(kOfxActionLoad, effect, nullptr, nullptr) == kOfxStatOK);
    assert(exported->mainEntry(kOfxActionDescribe, effect, nullptr, nullptr) == kOfxStatErrMissingHostFeature);
    put(handle(host_properties), kOfxImageEffectPropTemporalClipAccess, 0, 1);
    for (const auto* action : {kOfxActionDescribe, kOfxImageEffectActionDescribeInContext, kOfxActionCreateInstance}) {
        assert(exported->mainEntry(action, effect, nullptr, nullptr) == kOfxStatOK);
    }
    const double range[] = {10, 14};
    put_doubles(handle(source_properties), kOfxImageEffectPropFrameRange, 2, range);
    prepare_frames();
    for (double time : {12, 14, 10, 13, 11, 12}) {
        fetched.clear();
        put(handle(in_args), kOfxPropTime, 0, time);
        assert(exported->mainEntry(kOfxImageEffectActionGetFramesNeeded, effect, handle(in_args), handle(out_args)) == kOfxStatOK);
        double declared[2] = {};
        get_many(handle(out_args), "OfxImageClipPropFrameRange_Source", 2, declared);
        assert(declared[0] == std::max(10.0, time - 1) && declared[1] == std::min(14.0, time + 1));
        assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
        assert(outstanding == 0);
        assert(fetched.front() == time);
        assert(output_frame.pixels[0] == static_cast<float>(std::max(10.0, time - 1)));
        assert(output_frame.pixels[4 * 4] == static_cast<float>(time));
        assert(output_frame.pixels[8 * 4] == static_cast<float>(std::min(14.0, time + 1)));
    }
    // Bypass must preserve all bits, including signed zero, NaN and alpha.
    view = 1;
    put(handle(in_args), kOfxPropTime, 0, 12.0);
    assert(exported->mainEntry(kOfxImageEffectActionGetFramesNeeded, effect, handle(in_args), handle(out_args)) == kOfxStatOK);
    double original_range[2] = {};
    get_many(handle(out_args), "OfxImageClipPropFrameRange_Source", 2, original_range);
    assert(original_range[0] == 12 && original_range[1] == 12);
    auto& source = source_frames.at(12).pixels;
    source[0] = -0.0F; source[1] = std::nanf("42"); source[2] = -3; source[3] = 0.25F;
    fetched.clear();
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
    assert(fetched.size() == 1 && outstanding == 0);
    assert(!std::memcmp(source.data(), output_frame.pixels.data(), source.size() * sizeof(float)));
    // Negative row strides and nonzero image origins must be honoured for RGB and RGBA.
    for (int channels : {3, 4}) {
        prepare_frames(channels, true);
        assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
        for (int y = 0; y < height; ++y) {
            assert(!std::memcmp(output_frame.pixels.data() + y * width * channels,
                                source_frames.at(12).pixels.data() + (height - 1 - y) * width * channels,
                                width * channels * sizeof(float)));
        }
        assert(outstanding == 0);
    }
    view = 0;
    prepare_frames();
    missing_time = 11;
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatFailed);
    assert(outstanding == 0);
    missing_time = -1;
    aborted = true;
    fetched.clear();
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
    assert(fetched.empty() && outstanding == 0);
    aborted = false;
    offset = 0;
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatErrValue);
    offset = 1;
    // A one-frame clip requires only that one frame, regardless of offset.
    const double single_frame_range[] = {12, 12};
    put_doubles(handle(source_properties), kOfxImageEffectPropFrameRange, 2, single_frame_range);
    fetched.clear();
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
    assert(fetched.size() == 1 && fetched.front() == 12 && outstanding == 0);
    put_doubles(handle(source_properties), kOfxImageEffectPropFrameRange, 2, range);
    offset = 20;
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
    assert(output_frame.pixels[0] == 10 && output_frame.pixels[8 * 4] == 14 && outstanding == 0);
    const auto fractional = deflicker::frame_plan(-0.5, 1, -2.5, 1.5);
    assert((fractional == std::array<double,3>{-1.5, -0.5, 0.5}));
    bool invalid_range_rejected = false;
    try { deflicker::frame_plan(0, 1, 2, 1); }
    catch (const std::invalid_argument&) { invalid_range_rejected = true; }
    assert(invalid_range_rejected);
    assert(exported->mainEntry(kOfxImageEffectActionGetClipPreferences, effect, nullptr, handle(out_args)) == kOfxStatOK);
    int frame_varying = 0;
    get(handle(out_args), kOfxImageEffectFrameVarying, 0, &frame_varying);
    assert(frame_varying == 1);
    for (const auto* action : {kOfxImageEffectActionGetRegionOfDefinition, kOfxImageEffectActionGetRegionsOfInterest}) {
        assert(exported->mainEntry(action, effect, handle(in_args), handle(out_args)) == kOfxStatOK);
        const auto* key = !std::strcmp(action, kOfxImageEffectActionGetRegionOfDefinition)
                        ? kOfxImageEffectPropRegionOfDefinition : "OfxImageClipPropRoI_Source";
        double rectangle[4] = {};
        get_many(handle(out_args), key, 4, rectangle);
        assert(rectangle[0] == 10 && rectangle[1] == 20 && rectangle[2] == 22 && rectangle[3] == 24);
    }
    offset = 1;
    // Diagnostics must record a complete frame identity record when requested.
    const auto suffix = std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
    const auto log_path = std::filesystem::temp_directory_path() / ("ibd-ofx-host-test-" + suffix + ".jsonl");
    diagnostics_path = log_path.string();
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
    {
        std::ifstream log(log_path);
        std::string record;
        assert(std::getline(log, record));
        assert(record.find("\"requested\":[11,12,13]") != std::string::npos);
        assert(record.find("\"actual\":[11,12,13]") != std::string::npos);
        assert(record.find("\"hashes\":[") != std::string::npos);
    }
    std::filesystem::remove(log_path);
    diagnostics_path = (std::filesystem::temp_directory_path() / ("ibd-nonexistent-" + suffix) / "log.jsonl").string();
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatOK);
    diagnostics_path.clear();
    // Unsupported image formats fail while releasing images already fetched.
    put_string(handle(source_frames.at(12).properties), kOfxImageEffectPropPixelDepth, 0, kOfxBitDepthShort);
    assert(exported->mainEntry(kOfxImageEffectActionRender, effect, handle(in_args), nullptr) == kOfxStatErrImageFormat);
    assert(outstanding == 0);
    put_string(handle(source_frames.at(12).properties), kOfxImageEffectPropPixelDepth, 0, kOfxBitDepthFloat);
    assert(exported->mainEntry(kOfxActionDestroyInstance, effect, nullptr, nullptr) == kOfxStatOK);
    assert(outstanding == 0);
    std::cout << "PASS: lifecycle, host temporal capability, frame declarations, nonzero clip range,\n"
                 "clip boundaries, random request order, exact bypass, RGB/RGBA, signed strides,\n"
                 "missing-frame cleanup, cancellation, invalid parameters, single-frame clips,\n"
                 "large offsets, fractional times, geometry, diagnostic logging, unsupported formats\n"
                 "and image release.\n";
}
