#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
xcrun clang++ -std=c++20 -O1 -g -Wall -Wextra -Wpedantic -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -Ivendor/openfx tests/host_tests.cpp -o build/host_tests
./build/host_tests
