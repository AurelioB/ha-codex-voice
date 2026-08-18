cmake_minimum_required(VERSION 3.16)
project(codex-aec3-capture VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)
pkg_check_modules(ALSA REQUIRED IMPORTED_TARGET alsa)
pkg_check_modules(WEBRTC_APM REQUIRED IMPORTED_TARGET webrtc-audio-processing-1)

add_library(codex_aec3_core OBJECT
    src/capture_engine.cpp
    src/coherent_microphone_combiner.cpp
)
set_target_properties(codex_aec3_core PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    CXX_VISIBILITY_PRESET hidden
)
target_include_directories(codex_aec3_core PRIVATE
    include
    src
)
target_link_libraries(codex_aec3_core PRIVATE
    PkgConfig::ALSA
    PkgConfig::WEBRTC_APM
    Threads::Threads
)
target_compile_options(codex_aec3_core PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -ffunction-sections
    -fdata-sections
)

add_library(codex_aec3_capture SHARED
    src/c_api.cpp
    $<TARGET_OBJECTS:codex_aec3_core>
)
target_include_directories(codex_aec3_capture
    PUBLIC include
    PRIVATE src
)
target_link_libraries(codex_aec3_capture PRIVATE
    PkgConfig::ALSA
    PkgConfig::WEBRTC_APM
    Threads::Threads
)
target_link_options(codex_aec3_capture PRIVATE -Wl,--gc-sections)
set_target_properties(codex_aec3_capture PROPERTIES
    OUTPUT_NAME codex_aec3_capture
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
    BUILD_WITH_INSTALL_RPATH YES
    INSTALL_RPATH "$ORIGIN"
)

add_executable(codex_aec3_canary
    src/canary.cpp
    $<TARGET_OBJECTS:codex_aec3_core>
)
target_include_directories(codex_aec3_canary PRIVATE include src)
target_link_libraries(codex_aec3_canary PRIVATE
    PkgConfig::ALSA
    PkgConfig::WEBRTC_APM
    Threads::Threads
)
target_link_options(codex_aec3_canary PRIVATE -Wl,--gc-sections)
set_target_properties(codex_aec3_canary PROPERTIES
    BUILD_WITH_INSTALL_RPATH YES
    INSTALL_RPATH "$ORIGIN/../lib"
)

install(TARGETS codex_aec3_capture
    LIBRARY DESTINATION lib
)
install(TARGETS codex_aec3_canary
    RUNTIME DESTINATION bin
)
install(FILES include/codex_aec3_capture.h DESTINATION include)
