cmake_minimum_required(VERSION 3.16)
project(fat32defrag VERSION 1.5.1 LANGUAGES C)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

add_executable(fat32defrag src/fat32defrag.c)
find_package(Threads REQUIRED)
target_link_libraries(fat32defrag PRIVATE Threads::Threads)
target_compile_options(fat32defrag PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2)
target_compile_definitions(fat32defrag PRIVATE _FILE_OFFSET_BITS=64 _GNU_SOURCE)
install(TARGETS fat32defrag RUNTIME DESTINATION bin)
install(PROGRAMS gui/fat32defrag_gui.py DESTINATION lib/fat32defrag)
install(PROGRAMS gui/allocation_mapper.py gui/privileged_helper.py gui/exfat_engine.py DESTINATION lib/fat32defrag)
install(FILES gui/backends/__init__.py gui/backends/base.py gui/backends/registry.py gui/backends/fat_common.py gui/backends/fat12.py gui/backends/fat16.py gui/backends/fat32.py gui/backends/exfat.py gui/backends/ntfs.py gui/backends/ext4.py gui/backends/btrfs.py gui/backends/xfs.py gui/backends/swap.py gui/backends/ufs.py gui/backends/zfs.py gui/backends/affs.py gui/backends/minix.py DESTINATION lib/fat32defrag/backends)
install(PROGRAMS packaging/linux-defragger packaging/liux-defragger packaging/fat32defrag-gui DESTINATION bin)
install(FILES packaging/io.github.linuxdefragger.desktop DESTINATION share/applications)
install(FILES packaging/io.github.linuxdefragger.svg DESTINATION share/icons/hicolor/scalable/apps)

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    add_test(NAME fat32defrag-tests
             COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_tests.sh $<TARGET_FILE:fat32defrag>)
    add_test(NAME allocation-mapper-tests
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_allocation_mapper.py)
endif()
