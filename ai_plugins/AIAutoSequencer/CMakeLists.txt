cmake_minimum_required(VERSION 3.16)
project(AIAutoSequencer CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_library(AIAutoSequencer SHARED
    AIAutoSequencerService.cpp
)

target_include_directories(AIAutoSequencer PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party/xlights
)

# The plugin must not link back into xLights -- it only needs the OS.
if(WIN32)
    target_link_libraries(AIAutoSequencer PRIVATE ws2_32)
elseif(APPLE)
    # nothing extra
else()
    # libstdc++ / libc via the default toolchain
endif()

set_target_properties(AIAutoSequencer PROPERTIES
    PREFIX ""                 # AIAutoSequencer.dll / libAIAutoSequencer.dylib naming
    OUTPUT_NAME "AIAutoSequencer"
)

# xLights loads plugins from `ai_plugins/` next to the executable.
# Install straight there when XLIGHTS_DIR is provided:
#   cmake -DXLIGHTS_DIR=/path/to/xLights ..
#   cmake --build . --target install
if(DEFINED XLIGHTS_DIR)
    install(TARGETS AIAutoSequencer
        LIBRARY DESTINATION "${XLIGHTS_DIR}/ai_plugins"
        RUNTIME DESTINATION "${XLIGHTS_DIR}/ai_plugins"
    )
endif()
