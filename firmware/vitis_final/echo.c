/*
 * echo.c - UDP RX reassembly + UDP TX (lwIP RAW API)
 * - Host -> Board (입력): UDP/6001
 * - Board -> Host (출력): UDP/6002
 * - 기존 링버퍼 API(tcp_rx_* / start_sending / tcp_tx_is_busy) 유지
 */

#include <stdio.h>
#include <string.h>
#include "lwip/err.h"
#include "lwip/udp.h"
#include "lwip/pbuf.h"
#include "lwip/inet.h"
#include "netif/xadapter.h"

#if defined (__arm__) || defined (__aarch64__)
#include "sleep.h"
#endif
#include "xil_cache.h"

/* -------------------------------------------------------------------------- */
/* Config                                                                     */
/* -------------------------------------------------------------------------- */
#define UDP_RX_PORT     6001    /* Host -> Board (입력) */
#define UDP_TX_PORT     6002    /* Board -> Host (출력) */

#define IN_IMG_W        320
#define IN_IMG_H        180
#define IN_BPP          4
#define IN_FRAME_BYTES  (IN_IMG_W * IN_IMG_H * IN_BPP)

#define NUM_BUFFERS     64
#define CHUNK_PAYLOAD   1400    /* 안전 페이로드(조각화 방지) */
#define MAX_CHUNKS      700     /* 여유있게 잡음 */

/* UDP header (host와 동일하게 사용) */
#define UDP_MAGIC   0x55AA
#define UDP_KIND_IN  1 /* Host->Board */
#define UDP_KIND_OUT 2 /* Board->Host */

typedef struct __attribute__((__packed__)) {
    u16_t magic;        /* 0x55AA */
    u16_t kind;         /* 1=in, 2=out */
    u32_t frame_id;
    u16_t chunk_id;     /* 0..total_chunks-1 */
    u16_t total_chunks; /* 전체 청크 수 */
    u16_t payload_len;  /* 실제 페이로드 길이 */
    u16_t reserved;
} udp_hdr_t;

/* -------------------------------------------------------------------------- */
/* Globals                                                                    */
/* -------------------------------------------------------------------------- */
struct netif echo_netif;

/* UDP PCBs */
static struct udp_pcb *rx_pcb = NULL;
static struct udp_pcb *tx_pcb = NULL;

/* Peer(Host) 정보 */
static ip_addr_t host_ip;
static u16_t     host_tx_port = UDP_TX_PORT;
static volatile int peer_ready = 0;

/* RX 링버퍼: 그대로 유지 (프레임 단위) */
static u8 tcp_rx_buffers[NUM_BUFFERS][IN_FRAME_BYTES] __attribute__((aligned(64)));
static volatile u8  tcp_rx_ready[NUM_BUFFERS] = {0};
static volatile int tcp_rx_wr_idx = 0;
static volatile int tcp_rx_rd_idx = 0;
static volatile int tcp_rx_count  = 0;

/* TX busy 개념(UDP에선 비활성) */
static u8 tcp_tx_active_dummy = 0;

/* 입력 프레임 재조립 상태 */
static u32   rx_frame_id = 0xFFFFFFFF;
static u16_t rx_total = 0, rx_got = 0;
static u8   *rx_dst = NULL;
static u8    rx_bitmap[MAX_CHUNKS];

/* 출력 프레임 ID (Board->Host) */
static u32 g_frame_id_out = 0;

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */
static inline int rx_full(void)  { return (tcp_rx_count == NUM_BUFFERS); }
static inline int rx_empty(void) { return (tcp_rx_count == 0); }

/* 외부에서 피어 준비 여부 확인용 */
int udp_peer_is_ready(void) { return peer_ready != 0; }

/* Public: peek/pop RX frame (기존 인터페이스 유지) */
u8* tcp_rx_peek_frame(int *idx_out)
{
    if (rx_empty()) return NULL;
    if (tcp_rx_ready[tcp_rx_rd_idx] == 0) return NULL;
    if (idx_out) *idx_out = tcp_rx_rd_idx;
    return tcp_rx_buffers[tcp_rx_rd_idx];
}

int tcp_rx_pop_frame(void)
{
    if (rx_empty() || tcp_rx_ready[tcp_rx_rd_idx] == 0) return -1;
    tcp_rx_ready[tcp_rx_rd_idx] = 0;
    tcp_rx_rd_idx = (tcp_rx_rd_idx + 1) % NUM_BUFFERS;
    tcp_rx_count--;
    return 0;
}

/* -------------------------------------------------------------------------- */
/* UDP RX callback: 입력(Y) 프레임 조립                                        */
/* -------------------------------------------------------------------------- */
static void udp_rx_cb(void *arg, struct udp_pcb *upcb, struct pbuf *p,
                      const ip_addr_t *addr, u16_t port)
{
    if (!p) return;

    /* 첫 수신 시 피어 고정 */
    if (!peer_ready) {
        host_ip = *addr;
        host_tx_port = UDP_TX_PORT; /* 고정 포트 사용 */
        peer_ready = 1;
    }

    if (p->len < sizeof(udp_hdr_t)) { pbuf_free(p); return; }

    udp_hdr_t h;
    memcpy(&h, p->payload, sizeof(h));

    /* 엔디안 변환 */
    u16_t magic = ntohs(h.magic);
    u16_t kind  = ntohs(h.kind);
    u32_t fid   = ntohl(h.frame_id);
    u16_t cid   = ntohs(h.chunk_id);
    u16_t total = ntohs(h.total_chunks);
    u16_t pay   = ntohs(h.payload_len);

    if (magic != UDP_MAGIC || kind != UDP_KIND_IN) {
        pbuf_free(p); return;
    }
    if (total == 0 || total >= MAX_CHUNKS) {
        pbuf_free(p); return;
    }
    if (rx_full() && rx_dst == NULL) {
        /* 링이 가득 찬 상태에서 새 프레임 시작 불가 → 드롭 */
        pbuf_free(p); return;
    }

    /* 프레임 시작/전환 처리 */
    if (fid != rx_frame_id) {
        /* 이전 미완 프레임은 리셋 */
        rx_frame_id = fid;
        rx_total = total; rx_got = 0;
        memset(rx_bitmap, 0, sizeof(rx_bitmap));
        rx_dst = tcp_rx_buffers[tcp_rx_wr_idx];
    }

    /* 페이로드 복사 */
    u8 *payload = (u8 *)p->payload + sizeof(udp_hdr_t);
    u32 offset = (u32)cid * (u32)CHUNK_PAYLOAD;

    if (offset + pay <= IN_FRAME_BYTES && rx_dst != NULL) {
        memcpy(rx_dst + offset, payload, pay);
        if (!rx_bitmap[cid]) { rx_bitmap[cid] = 1; rx_got++; }
    }

    /* 프레임 완성되면 링에 게시 */
    if (rx_got == rx_total) {
        Xil_DCacheFlushRange((INTPTR)rx_dst, IN_FRAME_BYTES);
        tcp_rx_ready[tcp_rx_wr_idx] = 1;
        tcp_rx_count++;
        tcp_rx_wr_idx = (tcp_rx_wr_idx + 1) % NUM_BUFFERS;

        /* 다음 프레임 준비 */
        rx_dst = NULL;
        rx_frame_id = 0xFFFFFFFF;
        rx_total = rx_got = 0;
        memset(rx_bitmap, 0, sizeof(rx_bitmap));
    }

    pbuf_free(p);
}

/* -------------------------------------------------------------------------- */
/* UDP TX: 출력 프레임(BOARD->HOST) 전송                                      */
/* -------------------------------------------------------------------------- */
int start_sending(const u8 *buf, u32 len)
{
    if (!peer_ready || !tx_pcb) return -1;

    u16_t total = (len + CHUNK_PAYLOAD - 1) / CHUNK_PAYLOAD;

    for (u16_t cid = 0; cid < total; ++cid) {
        u16_t pay = (cid == total - 1) ? (len - (u32)cid*CHUNK_PAYLOAD) : CHUNK_PAYLOAD;

        struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT,
                                    (u16_t)(sizeof(udp_hdr_t) + pay),
                                    PBUF_RAM);
        if (!p) {
            /* 버퍼 부족 → 짧게 대기 후 재시도 */
            usleep(100);
            cid--;
            continue;
        }

        udp_hdr_t *h = (udp_hdr_t *)p->payload;
        h->magic        = htons(UDP_MAGIC);
        h->kind         = htons(UDP_KIND_OUT);
        h->frame_id     = htonl(g_frame_id_out);
        h->chunk_id     = htons(cid);
        h->total_chunks = htons(total);
        h->payload_len  = htons(pay);
        h->reserved     = 0;

        memcpy(((u8*)p->payload) + sizeof(udp_hdr_t),
               buf + (u32)cid*CHUNK_PAYLOAD, pay);
        err_t e = udp_sendto(tx_pcb, p, &host_ip, host_tx_port);
        pbuf_free(p);
        if (e != ERR_OK) {
            return -2;
        }
    }

    g_frame_id_out++;
    return 0;
}

/* UDP에서는 busy 개념 없음 → 항상 0 */
int tcp_tx_is_busy(void) { (void)tcp_tx_active_dummy; return 0; }

/* -------------------------------------------------------------------------- */
/* Start "application" (UDP 바인드/콜백 등록)                                 */
/* -------------------------------------------------------------------------- */
int start_application(void)
{
    /* RX PCB */
    rx_pcb = udp_new_ip_type(IPADDR_TYPE_ANY);
    if (!rx_pcb) return -1;
    if (udp_bind(rx_pcb, IP_ANY_TYPE, UDP_RX_PORT) != ERR_OK) {
        udp_remove(rx_pcb);
        rx_pcb = NULL;
        return -2;
    }
    udp_recv(rx_pcb, udp_rx_cb, NULL);

    /* TX PCB */
    tx_pcb = udp_new_ip_type(IPADDR_TYPE_ANY);
    if (!tx_pcb) {
        udp_remove(rx_pcb);
        rx_pcb = NULL;
        return -3;
    }

    /* 초기 상태 리셋 */
    peer_ready = 0;
    rx_frame_id = 0xFFFFFFFF;
    rx_total = rx_got = 0;
    rx_dst = NULL;
    memset(rx_bitmap, 0, sizeof(rx_bitmap));

    return 0;
}
