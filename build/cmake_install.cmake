# Install script for directory: /mnt/data/linux-defragger-1.8.0-32-source

# Set the install prefix
if(NOT DEFINED CMAKE_INSTALL_PREFIX)
  set(CMAKE_INSTALL_PREFIX "/usr/local")
endif()
string(REGEX REPLACE "/$" "" CMAKE_INSTALL_PREFIX "${CMAKE_INSTALL_PREFIX}")

# Set the install configuration name.
if(NOT DEFINED CMAKE_INSTALL_CONFIG_NAME)
  if(BUILD_TYPE)
    string(REGEX REPLACE "^[^A-Za-z0-9_]+" ""
           CMAKE_INSTALL_CONFIG_NAME "${BUILD_TYPE}")
  else()
    set(CMAKE_INSTALL_CONFIG_NAME "Release")
  endif()
  message(STATUS "Install configuration: \"${CMAKE_INSTALL_CONFIG_NAME}\"")
endif()

# Set the component getting installed.
if(NOT CMAKE_INSTALL_COMPONENT)
  if(COMPONENT)
    message(STATUS "Install component: \"${COMPONENT}\"")
    set(CMAKE_INSTALL_COMPONENT "${COMPONENT}")
  else()
    set(CMAKE_INSTALL_COMPONENT)
  endif()
endif()

# Install shared libraries without execute permission?
if(NOT DEFINED CMAKE_INSTALL_SO_NO_EXE)
  set(CMAKE_INSTALL_SO_NO_EXE "1")
endif()

# Is this installation the result of a crosscompile?
if(NOT DEFINED CMAKE_CROSSCOMPILING)
  set(CMAKE_CROSSCOMPILING "FALSE")
endif()

# Set path to fallback-tool for dependency-resolution.
if(NOT DEFINED CMAKE_OBJDUMP)
  set(CMAKE_OBJDUMP "/usr/bin/objdump")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/linux-defragger-engine" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/linux-defragger-engine")
    file(RPATH_CHECK
         FILE "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/linux-defragger-engine"
         RPATH "")
  endif()
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/bin" TYPE EXECUTABLE FILES "/mnt/data/linux-defragger-1.8.0-32-source/build/linux-defragger-engine")
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/linux-defragger-engine" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/linux-defragger-engine")
    if(CMAKE_INSTALL_DO_STRIP)
      execute_process(COMMAND "/usr/bin/strip" "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/linux-defragger-engine")
    endif()
  endif()
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/hfs_engine" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/hfs_engine")
    file(RPATH_CHECK
         FILE "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/hfs_engine"
         RPATH "")
  endif()
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/linux-defragger" TYPE EXECUTABLE FILES "/mnt/data/linux-defragger-1.8.0-32-source/build/hfs_engine")
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/hfs_engine" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/hfs_engine")
    if(CMAKE_INSTALL_DO_STRIP)
      execute_process(COMMAND "/usr/bin/strip" "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/hfs_engine")
    endif()
  endif()
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/linux-defragger" TYPE PROGRAM FILES
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/linux_defragger_gui.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/allocation_mapper.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/privileged_helper.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/exfat_engine.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/affs_engine.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/apple_engine.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/ntfs_engine.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/native_compact_engine.py"
    )
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/linux-defragger" TYPE FILE FILES "/mnt/data/linux-defragger-1.8.0-32-source/gui/version.py")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/backends" TYPE FILE FILES
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/__init__.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/base.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/registry.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/fat_common.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/fat12.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/fat16.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/fat32.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/exfat.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/ntfs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/ext4.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/btrfs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/xfs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/swap.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/ufs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/zfs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/affs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/minix.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/hfs.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/hfsplus.py"
    "/mnt/data/linux-defragger-1.8.0-32-source/gui/backends/apfs.py"
    )
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/bin" TYPE PROGRAM FILES "/mnt/data/linux-defragger-1.8.0-32-source/packaging/linux-defragger")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/bin" TYPE PROGRAM RENAME "linux-defragger-testdata" FILES "/mnt/data/linux-defragger-1.8.0-32-source/tools/linux-defragger-testdata.py")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/share/applications" TYPE FILE FILES "/mnt/data/linux-defragger-1.8.0-32-source/packaging/io.github.linuxdefragger.desktop")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/share/icons/hicolor/scalable/apps" TYPE FILE FILES "/mnt/data/linux-defragger-1.8.0-32-source/packaging/io.github.linuxdefragger.svg")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/linux-defragger/vendor" TYPE DIRECTORY FILES "/mnt/data/linux-defragger-1.8.0-32-source/vendor/amitools" REGEX "/\\_\\_pycache\\_\\_$" EXCLUDE REGEX "/[^/]*\\.pyc$" EXCLUDE)
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/share/doc/linux-defragger" TYPE FILE FILES
    "/mnt/data/linux-defragger-1.8.0-32-source/README.md"
    "/mnt/data/linux-defragger-1.8.0-32-source/RELEASE_NOTES.md"
    "/mnt/data/linux-defragger-1.8.0-32-source/TEST_STATUS.md"
    )
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "Unspecified" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/share/doc/linux-defragger" TYPE FILE RENAME "COPYING.hfsutils" FILES "/mnt/data/linux-defragger-1.8.0-32-source/vendor/hfsutils-3.2.6/COPYING")
endif()

string(REPLACE ";" "\n" CMAKE_INSTALL_MANIFEST_CONTENT
       "${CMAKE_INSTALL_MANIFEST_FILES}")
if(CMAKE_INSTALL_LOCAL_ONLY)
  file(WRITE "/mnt/data/linux-defragger-1.8.0-32-source/build/install_local_manifest.txt"
     "${CMAKE_INSTALL_MANIFEST_CONTENT}")
endif()
if(CMAKE_INSTALL_COMPONENT)
  if(CMAKE_INSTALL_COMPONENT MATCHES "^[a-zA-Z0-9_.+-]+$")
    set(CMAKE_INSTALL_MANIFEST "install_manifest_${CMAKE_INSTALL_COMPONENT}.txt")
  else()
    string(MD5 CMAKE_INST_COMP_HASH "${CMAKE_INSTALL_COMPONENT}")
    set(CMAKE_INSTALL_MANIFEST "install_manifest_${CMAKE_INST_COMP_HASH}.txt")
    unset(CMAKE_INST_COMP_HASH)
  endif()
else()
  set(CMAKE_INSTALL_MANIFEST "install_manifest.txt")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  file(WRITE "/mnt/data/linux-defragger-1.8.0-32-source/build/${CMAKE_INSTALL_MANIFEST}"
     "${CMAKE_INSTALL_MANIFEST_CONTENT}")
endif()
