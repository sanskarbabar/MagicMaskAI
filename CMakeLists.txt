cmake_minimum_required(VERSION 3.20)
project(AICutout LANGUAGES CXX VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()

option(AICUTOUT_HOST_VALIDATION "Keep the OpenFX Support library's host-property validation (tests use ON, production OFF)" OFF)

set(OFX_THIRD ${CMAKE_SOURCE_DIR}/plugin/OFX/third_party/openfx)
set(CORE_SRC ${CMAKE_SOURCE_DIR}/core/native/acm.cpp ${CMAKE_SOURCE_DIR}/core/native/edge.cpp)

# ---- native core as a shared library for Python (ctypes): Render/export + Companion preview + format parity tests
add_library(aicutout_core SHARED ${CORE_SRC} core/native/capi.cpp)
target_include_directories(aicutout_core PRIVATE core/native)
set_target_properties(aicutout_core PROPERTIES PREFIX "" RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/native)

# ---- OpenFX plugin
file(GLOB OFX_SUPPORT_SRC
  ${OFX_THIRD}/support_library/ofxsCore.cpp ${OFX_THIRD}/support_library/ofxsImageEffect.cpp
  ${OFX_THIRD}/support_library/ofxsInteract.cpp ${OFX_THIRD}/support_library/ofxsLog.cpp
  ${OFX_THIRD}/support_library/ofxsMultiThread.cpp ${OFX_THIRD}/support_library/ofxsParams.cpp
  ${OFX_THIRD}/support_library/ofxsProperty.cpp ${OFX_THIRD}/support_library/ofxsPropertyValidation.cpp)

add_library(AICutout MODULE plugin/OFX/AICutout.cpp plugin/OFX/service_link.cpp ${CORE_SRC} ${OFX_SUPPORT_SRC})
target_include_directories(AICutout PRIVATE ${OFX_THIRD}/include ${OFX_THIRD}/support_include core/native plugin/OFX)
target_compile_definitions(AICutout PRIVATE WIN32)
if(NOT AICUTOUT_HOST_VALIDATION)
  target_compile_definitions(AICutout PRIVATE kOfxsDisableValidation)
endif()
target_link_libraries(AICutout PRIVATE ws2_32)
if(MINGW)
  # no runtime DLL dependencies next to the plugin
  target_link_options(AICutout PRIVATE -static)
  target_link_options(aicutout_core PRIVATE -static)
endif()
# OpenFX bundle layout: AICutout.ofx.bundle/Contents/Win64/AICutout.ofx
set_target_properties(AICutout PROPERTIES
  PREFIX "" SUFFIX ".ofx"
  LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/plugin/AICutout.ofx.bundle/Contents/Win64
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/plugin/AICutout.ofx.bundle/Contents/Win64)
foreach(cfg DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
  set_target_properties(AICutout PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY_${cfg} ${CMAKE_BINARY_DIR}/plugin/AICutout.ofx.bundle/Contents/Win64
    RUNTIME_OUTPUT_DIRECTORY_${cfg} ${CMAKE_BINARY_DIR}/plugin/AICutout.ofx.bundle/Contents/Win64)
endforeach()

# ---- mock OpenFX host used by tests/test_ofx_host.py
add_executable(mock_host tests/host/mock_host.cpp)
target_include_directories(mock_host PRIVATE ${OFX_THIRD}/include)
target_compile_definitions(mock_host PRIVATE WIN32)
if(MINGW)
  target_link_options(mock_host PRIVATE -static)
endif()
