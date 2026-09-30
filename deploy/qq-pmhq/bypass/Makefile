CFLAGS = -Wall -O2 -fPIC
all: libpmhqwrap.so tlsserver pmhq patch_libpmhq
libpmhqwrap.so: libpmhqwrap.c
	gcc $(CFLAGS) -shared -o $@ $< -ldl
tlsserver: tlsserver.c
	gcc $(CFLAGS) -o $@ $< -lssl -lcrypto -lpthread
pmhq: launcher.c
	gcc $(CFLAGS) -o $@ $<
patch_libpmhq: patch_libpmhq.c
	gcc $(CFLAGS) -o $@ $<
clean:
	rm -f libpmhqwrap.so tlsserver pmhq patch_libpmhq
.PHONY: all clean
