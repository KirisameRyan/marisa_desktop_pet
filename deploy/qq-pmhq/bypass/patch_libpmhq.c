#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static const unsigned char Z0[] = {0x0f,0x84,0x1f,0x01,0x00,0x00};
static const unsigned char Z1[] = {0xe9,0x20,0x01,0x00,0x00,0x90};
int main(int Z2, char **Z3) {
    if (Z2 < 2) { fprintf(stderr, "usage: %s <libpmhq.so>\n", Z3[0]); return 1; }
    FILE *Z4 = fopen(Z3[1], "r+b"); if (!Z4) { perror("fopen"); return 1; }
    unsigned char Z5[6];
    fseek(Z4, 0x76326, SEEK_SET);
    if (fread(Z5, 1, 6, Z4) != 6) { fprintf(stderr, "read fail\n"); fclose(Z4); return 1; }
    if (memcmp(Z5, Z1, 6) == 0) { printf("already patched\n"); fclose(Z4); return 0; }
    if (memcmp(Z5, Z0, 6) != 0) { printf("unexpected bytes: %02x%02x%02x%02x%02x%02x\n", Z5[0],Z5[1],Z5[2],Z5[3],Z5[4],Z5[5]); fclose(Z4); return 1; }
    fseek(Z4, 0x76326, SEEK_SET);
    if (fwrite(Z1, 1, 6, Z4) != 6) { fprintf(stderr, "write fail\n"); fclose(Z4); return 1; }
    fclose(Z4); printf("patched OK\n"); return 0;
}
