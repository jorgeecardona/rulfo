#ifndef RULFO_TEST_CHECK_H
#define RULFO_TEST_CHECK_H

#include <stdio.h>
#include <stdlib.h>

#define CHECK(condition)                                                            \
    do {                                                                            \
        if (!(condition)) {                                                         \
            fprintf(stderr, "%s:%d: failed: %s\n", __FILE__, __LINE__, #condition); \
            exit(1);                                                                \
        }                                                                           \
    } while (0)

#endif
