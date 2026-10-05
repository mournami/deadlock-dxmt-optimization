#pragma once
#include <string_view>

namespace dxmt {
enum class ReportMode { Off, Light, Full };
inline ReportMode reportMode(std::string_view value) {
  if (value == "off") return ReportMode::Off;
  if (value == "full") return ReportMode::Full;
  return ReportMode::Light;
}
} // namespace dxmt
