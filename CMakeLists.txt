cmake_minimum_required(VERSION 3.16)
project(linux_defragger VERSION 1.5.2 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)

add_executable(linux-defragger-engine src/linux_defragger_engine.c)
target_link_libraries(linux-defragger-engine PRIVATE Threads::Threads)
target_compile_options(linux-defragger-engine PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2)
target_compile_definitions(linux-defragger-engine PRIVATE _FILE_OFFSET_BITS=64 _GNU_SOURCE)

install(TARGETS linux-defragger-engine RUNTIME DESTINATION bin)
install(PROGRAMS gui/linux_defragger_gui.py gui/allocation_mapper.py gui/privileged_helper.py gui/exfat_engine.py DESTINATION lib/linux-defragger)
install(FILES
    gui/backends/__init__.py gui/backends/base.py gui/backends/registry.py
    gui/backends/fat_common.py gui/backends/fat12.py gui/backends/fat16.py
    gui/backends/fat32.py gui/backends/exfat.py gui/backends/ntfs.py
    gui/backends/ext4.py gui/backends/btrfs.py gui/backends/xfs.py
    gui/backends/swap.py gui/backends/ufs.py gui/backends/zfs.py
    gui/backends/affs.py gui/backends/minix.py
    DESTINATION lib/linux-defragger/backends)
install(PROGRAMS packaging/linux-defragger DESTINATION bin)
install(FILES packaging/io.github.linuxdefragger.desktop DESTINATION share/applications)
install(FILES packaging/io.github.linuxdefragger.svg DESTINATION share/icons/hicolor/scalable/apps)

include(CTest)
if(BUILD_TESTING)
    add_test(NAME linux-defragger-tests
             COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_tests.sh $<TARGET_FILE:linux-defragger-engine>)
endif()
