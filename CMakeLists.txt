cmake_minimum_required(VERSION 3.16)
project(linux_defragger VERSION 1.8.0 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)

add_executable(linux-defragger-engine src/linux_defragger_engine.c)
target_link_libraries(linux-defragger-engine PRIVATE Threads::Threads)
target_compile_options(linux-defragger-engine PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2)
target_compile_definitions(linux-defragger-engine PRIVATE _FILE_OFFSET_BITS=64 _GNU_SOURCE)

set(HFSUTILS_DIR ${CMAKE_CURRENT_SOURCE_DIR}/vendor/hfsutils-3.2.6)
set(HFSUTILS_LIB_DIR ${HFSUTILS_DIR}/libhfs)
add_library(linux-defragger-hfsutils STATIC
    ${HFSUTILS_LIB_DIR}/block.c
    ${HFSUTILS_LIB_DIR}/btree.c
    ${HFSUTILS_LIB_DIR}/data.c
    ${HFSUTILS_LIB_DIR}/file.c
    ${HFSUTILS_LIB_DIR}/hfs.c
    ${HFSUTILS_LIB_DIR}/low.c
    ${HFSUTILS_LIB_DIR}/medium.c
    ${HFSUTILS_LIB_DIR}/memcmp.c
    ${HFSUTILS_LIB_DIR}/node.c
    ${HFSUTILS_LIB_DIR}/os.c
    ${HFSUTILS_LIB_DIR}/record.c
    ${HFSUTILS_LIB_DIR}/version.c
    ${HFSUTILS_LIB_DIR}/volume.c)
target_include_directories(linux-defragger-hfsutils PUBLIC ${HFSUTILS_DIR} ${HFSUTILS_LIB_DIR})
target_compile_options(linux-defragger-hfsutils PRIVATE -std=gnu89 -Wno-implicit-int -Wno-implicit-function-declaration -Wno-int-conversion)
target_compile_definitions(linux-defragger-hfsutils PRIVATE HAVE_CONFIG_H _FILE_OFFSET_BITS=64)

add_executable(linux-defragger-hfs-engine src/hfs_engine.c)
target_link_libraries(linux-defragger-hfs-engine PRIVATE linux-defragger-hfsutils)
target_compile_options(linux-defragger-hfs-engine PRIVATE -std=gnu89)
target_compile_definitions(linux-defragger-hfs-engine PRIVATE HAVE_CONFIG_H _FILE_OFFSET_BITS=64)
set_target_properties(linux-defragger-hfs-engine PROPERTIES OUTPUT_NAME hfs_engine)

install(TARGETS linux-defragger-engine RUNTIME DESTINATION bin)
install(TARGETS linux-defragger-hfs-engine RUNTIME DESTINATION lib/linux-defragger)
install(PROGRAMS gui/linux_defragger_gui.py gui/allocation_mapper.py gui/privileged_helper.py
                 gui/exfat_engine.py gui/affs_engine.py gui/apple_engine.py gui/ntfs_engine.py
        DESTINATION lib/linux-defragger)
install(FILES gui/version.py DESTINATION lib/linux-defragger)
install(FILES
    gui/backends/__init__.py gui/backends/base.py gui/backends/registry.py
    gui/backends/fat_common.py gui/backends/fat12.py gui/backends/fat16.py
    gui/backends/fat32.py gui/backends/exfat.py gui/backends/ntfs.py
    gui/backends/ext4.py gui/backends/btrfs.py gui/backends/xfs.py
    gui/backends/swap.py gui/backends/ufs.py gui/backends/zfs.py
    gui/backends/affs.py gui/backends/minix.py gui/backends/hfs.py
    gui/backends/hfsplus.py gui/backends/apfs.py
    DESTINATION lib/linux-defragger/backends)
install(PROGRAMS packaging/linux-defragger DESTINATION bin)
install(PROGRAMS tools/linux-defragger-testdata.py DESTINATION bin RENAME linux-defragger-testdata)
install(FILES packaging/io.github.linuxdefragger.desktop DESTINATION share/applications)
install(FILES packaging/io.github.linuxdefragger.svg DESTINATION share/icons/hicolor/scalable/apps)

include(CTest)
if(BUILD_TESTING)
    add_test(NAME linux-defragger-tests
             COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_tests.sh $<TARGET_FILE:linux-defragger-engine>)
endif()

install(DIRECTORY vendor/amitools DESTINATION lib/linux-defragger/vendor
        PATTERN "__pycache__" EXCLUDE
        PATTERN "*.pyc" EXCLUDE)
install(FILES README.md RELEASE_NOTES.md TEST_STATUS.md DESTINATION share/doc/linux-defragger)
install(FILES vendor/hfsutils-3.2.6/COPYING DESTINATION share/doc/linux-defragger RENAME COPYING.hfsutils)
