// New integration code. See ../NOTICE.md for licensing status.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>

namespace deflicker {
inline std::array<double, 3> frame_plan(double time, int offset, double first, double last) {
    if (!std::isfinite(time) || !std::isfinite(first) || !std::isfinite(last) || first > last || offset < 1) {
        throw std::invalid_argument("Invalid source frame range or frame offset");
    }
    return {std::clamp(time - offset, first, last), std::clamp(time, first, last),
            std::clamp(time + offset, first, last)};
}
}
