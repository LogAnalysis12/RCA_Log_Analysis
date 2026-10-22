#pragma once

#define RELEASE_AND_CLEAR(handle) do { (handle).reset(); } while (false)
