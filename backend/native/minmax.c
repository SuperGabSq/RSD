/* Min/max decimation in C, bound with ctypes.
 *
 * This is the same reduction as `backend/domain/decimation.py` performs with numpy:
 * split `n` samples into `columns` near-equal buckets and emit the smallest and largest
 * of each, interleaved. numpy already does it in C at roughly 30 us a frame, so the
 * point of this file is not speed -- it is that the boundary between Python and a
 * compiled library is a thing this project has to demonstrate, and a 40-line kernel is
 * the honest size for it.
 *
 * The bucket edges are computed exactly as the numpy path computes them
 * (`(i * n) / columns`, integer division) so the two produce byte-identical output.
 * A property test over random buffers holds them to that.
 *
 * Build (the Dockerfile does this; the Python side falls back to numpy if it is absent):
 *     cc -O2 -shared -fPIC -o libminmax.so minmax.c
 */

#include <stdint.h>
#include <stddef.h>

/* out must have room for 2 * columns int32s: [min0, max0, min1, max1, ...] */
void minmax_decimate(const int32_t *samples, int64_t n, int64_t columns, int32_t *out)
{
    if (n <= 0 || columns <= 0) {
        return;
    }

    for (int64_t i = 0; i < columns; i++) {
        /* Same edges as numpy's `(arange(columns) * n) // columns`. The multiply is done
         * in int64 because n * columns overflows int32 for any realistic frame. */
        int64_t start = (i * n) / columns;
        int64_t end = ((i + 1) * n) / columns;
        if (end <= start) {
            end = start + 1; /* never emit an empty bucket; matches reduceat's behaviour */
        }

        int32_t lo = samples[start];
        int32_t hi = lo;
        for (int64_t k = start + 1; k < end; k++) {
            int32_t v = samples[k];
            if (v < lo) {
                lo = v;
            }
            if (v > hi) {
                hi = v;
            }
        }

        out[2 * i] = lo;
        out[2 * i + 1] = hi;
    }
}
