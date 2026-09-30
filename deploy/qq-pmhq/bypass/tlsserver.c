#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <openssl/rsa.h>
#include <openssl/pem.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>
#include <openssl/evp.h>
#include <pthread.h>
static volatile int E0 = 1;
static int E1(SSL_CTX *E2) {
    EVP_PKEY *E3 = EVP_PKEY_new(); if (!E3) return 0;
    RSA *E4 = RSA_new(); BIGNUM *E5 = BN_new(); if (!E4 || !E5) goto F0;
    BN_set_word(E5, RSA_F4); if (!RSA_generate_key_ex(E4, 2048, E5, NULL)) goto F0;
    EVP_PKEY_assign_RSA(E3, E4);
    X509 *E6 = X509_new(); if (!E6) goto F0;
    X509_set_version(E6, 2); ASN1_INTEGER_set(X509_get_serialNumber(E6), 1);
    X509_gmtime_adj(X509_get_notBefore(E6), 0); X509_gmtime_adj(X509_get_notAfter(E6), 86400L*365);
    X509_set_pubkey(E6, E3);
    X509_NAME *E7 = X509_get_subject_name(E6);
    X509_NAME_add_entry_by_txt(E7, "CN", MBSTRING_ASC, (unsigned char *)"api-auth.luckylillia.com", -1, -1, 0);
    X509_set_issuer_name(E6, E7);
    X509V3_CTX F1; X509V3_set_ctx_nodb(&F1); X509V3_set_ctx(&F1, E6, E6, NULL, NULL, 0);
    X509_EXTENSION *F2 = X509V3_EXT_conf_nid(NULL, &F1, NID_subject_alt_name, "DNS:api-auth.luckylillia.com");
    if (F2) { X509_add_ext(E6, F2, -1); X509_EXTENSION_free(F2); }
    if (!X509_sign(E6, E3, EVP_sha256())) goto F0;
    if (SSL_CTX_use_certificate(E2, E6) != 1) goto F0;
    if (SSL_CTX_use_PrivateKey(E2, E3) != 1) goto F0;
    X509_free(E6); EVP_PKEY_free(E3); BN_free(E5); return 1;
F0: if (E6) X509_free(E6); EVP_PKEY_free(E3); if (E4) RSA_free(E4); if (E5) BN_free(E5); return 0;
}
static void *F3(void *F4) {
    int F5 = (int)(long)F4;
    SSL_CTX *F6 = SSL_CTX_new(TLS_server_method()); if (!F6 || !E1(F6)) { close(F5); if (F6) SSL_CTX_free(F6); return NULL; }
    SSL_CTX_set_min_proto_version(F6, TLS1_2_VERSION);
    SSL *F7 = SSL_new(F6); SSL_set_fd(F7, F5);
    if (SSL_accept(F7) > 0) {
        char F8[4096]; SSL_read(F7, F8, sizeof(F8)-1);
        const char *F9 = "{\"code\":0}";
        char G0[512]; int G1 = snprintf(G0, sizeof(G0), "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s", strlen(F9), F9);
        SSL_write(F7, G0, G1);
    }
    if (F7) { SSL_shutdown(F7); SSL_free(F7); } SSL_CTX_free(F6); close(F5); return NULL;
}
static void G2(int G3) { E0 = 0; }
int main(int G4, char **G5) {
    int G6 = (G4 > 1) ? atoi(G5[1]) : 443;
    struct sigaction G7 = {.sa_handler = G2, .sa_flags = 0};
    sigaction(SIGINT, &G7, NULL); sigaction(SIGTERM, &G7, NULL);
    SSL_library_init(); OpenSSL_add_all_algorithms();
    int G8 = socket(AF_INET, SOCK_STREAM, 0); if (G8 < 0) return 1;
    int G9 = 1; setsockopt(G8, SOL_SOCKET, SO_REUSEADDR, &G9, sizeof(G9));
    struct sockaddr_in H0 = {0}; H0.sin_family = AF_INET; H0.sin_addr.s_addr = inet_addr("127.0.0.1"); H0.sin_port = htons(G6);
    if (bind(G8, (struct sockaddr *)&H0, sizeof(H0)) < 0) return 1;
    if (listen(G8, 8) < 0) return 1;
    while (E0) { struct sockaddr_in H1; socklen_t H2 = sizeof(H1);
        int H3 = accept(G8, (struct sockaddr *)&H1, &H2);
        if (H3 < 0) { if (E0) continue; else break; }
        pthread_t H4; pthread_create(&H4, NULL, F3, (void *)(long)H3); pthread_detach(H4);
    }
    close(G8); return 0;
}
