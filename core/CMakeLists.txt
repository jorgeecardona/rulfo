cmake_minimum_required(VERSION 3.20)
project(rulfo LANGUAGES C)

option(RULFO_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
option(RULFO_FUZZ "Build the libFuzzer target (clang only)" OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_compile_options(-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wstrict-prototypes -Werror)

if(RULFO_SANITIZE OR RULFO_FUZZ)
  set(sanitizers -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer -g)
  add_compile_options(${sanitizers})
  add_link_options(${sanitizers})
endif()

add_library(rulfo STATIC src/frame.c)
target_include_directories(rulfo PUBLIC include)

enable_testing()
add_executable(test_frame tests/test_frame.c)
target_link_libraries(test_frame PRIVATE rulfo)
add_test(NAME frame COMMAND test_frame)

if(RULFO_FUZZ)
  # Coverage counters in the library, the fuzzer's main in the target.
  target_compile_options(rulfo PRIVATE -fsanitize=fuzzer-no-link)
  add_executable(fuzz_frame fuzz/fuzz_frame.c)
  target_link_libraries(fuzz_frame PRIVATE rulfo)
  target_link_options(fuzz_frame PRIVATE -fsanitize=fuzzer)
endif()
