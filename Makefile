# GTA IV subtitle mod — build (32-bit Windows DLL loaded as .asi)

CROSS    ?= i686-w64-mingw32-
CXX      := $(CROSS)g++
CXXFLAGS ?= -std=c++17 -O2 -fno-exceptions -fno-rtti -Wall -Wextra
LDFLAGS  ?= -shared -static-libgcc -static-libstdc++ -Wl,--no-insert-timestamp

BUILD := build
TARGET := $(BUILD)/GTAIV.SubtitleTest.asi

all: $(TARGET)

$(TARGET): src/subtitle_test.cpp src/selfrender.hxx | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/subtitle_test.cpp $(LDFLAGS)

$(BUILD):
	mkdir -p $@

clean:
	rm -rf $(BUILD)

.PHONY: all clean
