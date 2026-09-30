#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <link.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
static const unsigned char A0[7] = {0xc6,0x07,0x16,0x48,0x89,0xf8,0xc3};
#define A1 0xB82AB
#define A2 0x164430ULL
static int A3 = 0;
static int A4(void) { const char *p = getenv("PMHQ_TLS_PORT"); return p ? atoi(p) : 8443; }
static int A5(struct dl_phdr_info *A6, size_t A7, void *A8) {
    unsigned long *A9 = (unsigned long *)A8;
    for (int i = 0; i < A6->dlpi_phnum; i++)
        if (A6->dlpi_phdr[i].p_type == PT_LOAD && A6->dlpi_phdr[i].p_vaddr == A2)
        { *A9 = A6->dlpi_addr + A2; return 1; }
    return 0;
}
static void B0(void) {
    if (A3) return;
    unsigned long B1 = 0; dl_iterate_phdr(A5, &B1);
    if (!B1) { FILE *B2 = fopen("/proc/self/maps", "r"); if (B2) { char B3[512];
        while (fgets(B3, sizeof(B3), B2)) { unsigned long B4, B5;
            if (strstr(B3, "00:00 0") && strstr(B3, "r-xp") && sscanf(B3, "%lx-%lx", &B4, &B5) == 2 && (B5-B4) > 1000000) { B1 = B4; break; } }
        fclose(B2); } }
    if (!B1) return;
    unsigned long B6 = B1 + A1;
    long B7 = B6 & ~0xfffUL;
    if (mprotect((void *)B7, 0x1000, PROT_READ|PROT_WRITE|PROT_EXEC) != 0) return;
    memcpy((void *)B6, A0, 7);
    mprotect((void *)B7, 0x1000, PROT_READ|PROT_EXEC);
    A3 = 1;
}
int getaddrinfo(const char *B8, const char *B9, const struct addrinfo *C0, struct addrinfo **C1) {
    static int (*C2)(const char*,const char*,const struct addrinfo*,struct addrinfo**);
    if (!C2) C2 = dlsym(RTLD_NEXT, "getaddrinfo");
    if (B8 != NULL && strcmp(B8, "api-auth.luckylillia.com") == 0)
        return C2("127.0.0.1", B9, C0, C1);
    return C2(B8, B9, C0, C1);
}
int connect(int C3, const struct sockaddr *C4, socklen_t C5) {
    static int (*C6)(int, const struct sockaddr*, socklen_t);
    if (!C6) C6 = dlsym(RTLD_NEXT, "connect");
    struct sockaddr_storage C7;
    if (C4 && C5 <= sizeof(C7)) {
        memcpy(&C7, C4, C5);
        if (C4->sa_family == AF_INET) {
            struct sockaddr_in *C8 = (struct sockaddr_in *)&C7;
            if (ntohs(C8->sin_port) == 443 && C8->sin_addr.s_addr == inet_addr("127.0.0.1")) {
                if (!A3) B0(); C8->sin_port = htons(A4());
                return C6(C3, (struct sockaddr *)&C7, C5);
            }
        } else if (C4->sa_family == AF_INET6) {
            struct sockaddr_in6 *C9 = (struct sockaddr_in6 *)&C7;
            int D0 = 1; for (int i = 0; i < 16; i++) if (C9->sin6_addr.s6_addr[i] != (i==15?1:0)) { D0 = 0; break; }
            if (ntohs(C9->sin6_port) == 443 && D0) {
                if (!A3) B0(); C9->sin6_port = htons(A4());
                return C6(C3, (struct sockaddr *)&C7, C5);
            }
        }
    }
    return C6(C3, C4, C5);
}
