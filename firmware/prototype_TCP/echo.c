/*
 * echo.c - TCP RX ring buffer + backpressure (lwIP RAW API)
 */

#include <stdio.h>
#include <string.h>
#include "lwip/err.h"
#include "lwip/tcp.h"
#include "netif/xadapter.h"

#if defined (__arm__) || defined (__aarch64__)
#include "sleep.h"
#endif
#include "xil_cache.h"

/* -------------------------------------------------------------------------- */
/* Config                                                                     */
/* -------------------------------------------------------------------------- */
#define TCP_PORT        6001
#define IN_IMG_W        320
#define IN_IMG_H        180
#define IN_BPP          4
#define IN_FRAME_BYTES  (IN_IMG_W * IN_IMG_H * IN_BPP)
#define NUM_BUFFERS     10
#define TCP_TX_CHUNK    1460    // safe MSS chunk

/* -------------------------------------------------------------------------- */
/* Globals                                                                    */
/* -------------------------------------------------------------------------- */
struct netif echo_netif;
struct tcp_pcb *client_pcb = NULL;

/* RX buffers */
static u8 tcp_rx_buffers[NUM_BUFFERS][IN_FRAME_BYTES] __attribute__((aligned(64)));
static volatile u8  tcp_rx_ready[NUM_BUFFERS] = {0};
static volatile int tcp_rx_wr_idx = 0;
static volatile int tcp_rx_rd_idx = 0;
static volatile int tcp_rx_count  = 0;
static u32 tcp_rx_offset = 0;

/* TX async state */
static u8 *tcp_tx_buf_ptr = NULL;
static u32 tcp_tx_buf_len = 0;
static u32 tcp_tx_sent_len = 0;
static u8 tcp_tx_active = 0;

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */
static inline int rx_full(void)  { return (tcp_rx_count == NUM_BUFFERS); }
static inline int rx_empty(void) { return (tcp_rx_count == 0); }

/* -------------------------------------------------------------------------- */
/* Public: peek/pop RX frame                                                  */
/* -------------------------------------------------------------------------- */
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
/* TX: start async send */
/* -------------------------------------------------------------------------- */
err_t send_callback(void *arg, struct tcp_pcb *tpcb, u16_t len)
{
    if (!tcp_tx_active) return ERR_OK;

    while (tcp_tx_sent_len < tcp_tx_buf_len) {
        u16_t sndbuf = tcp_sndbuf(tpcb);
        if (sndbuf == 0) {
            // No space, wait for next ACK
            return ERR_OK;
        }

        u32 remain = tcp_tx_buf_len - tcp_tx_sent_len;
        u32 chunk  = (remain > TCP_TX_CHUNK) ? TCP_TX_CHUNK : remain;
        if (chunk > sndbuf) chunk = sndbuf;

        err_t e = tcp_write(tpcb,
                            tcp_tx_buf_ptr + tcp_tx_sent_len,
                            chunk,
                            TCP_WRITE_FLAG_COPY);
        if (e == ERR_OK) {
            tcp_tx_sent_len += chunk;
            tcp_output(tpcb);   // flush every chunk
        } else if (e == ERR_MEM) {
            // lwIP buffer full, retry on next tcp_sent()
            return ERR_OK;
        } else {
            tcp_tx_active = 0;
            return e;
        }
    }

    // If we sent the whole frame, mark TX done
    if (tcp_tx_sent_len >= tcp_tx_buf_len) {
        tcp_tx_active = 0;      // busy clear
    }

    return ERR_OK;
}

int start_sending(const u8 *buf, u32 len)
{
    if (!client_pcb || tcp_tx_active) return -1;

    tcp_tx_buf_ptr   = (u8 *)buf;
    tcp_tx_buf_len   = len;
    tcp_tx_sent_len  = 0;
    tcp_tx_active    = 1;

    // Kick-off by trying the first chunk
    return send_callback(NULL, client_pcb, 0);
}

int tcp_tx_is_busy(void) { return tcp_tx_active != 0; }

/* -------------------------------------------------------------------------- */
/* TX: send buffer to client                                                  */
/* -------------------------------------------------------------------------- */
int transfer_data(u8 *buffer, int length) {
    // Check if client is connected
    if (client_pcb == NULL) {
        return -1;
    }

    err_t err;
    int remaining = length;   // Use the given length instead of FRAME_BYTES
    int offset = 0;
    const int MAX_TCP_CHUNK = 1460;  // Maximum TCP payload size (close to MTU)


    while (remaining > 0) {
        // Decide the size of current chunk
        int chunk = (remaining > MAX_TCP_CHUNK) ? MAX_TCP_CHUNK : remaining;

        // Wait until TCP send buffer has enough space for the chunk
        while (tcp_sndbuf(client_pcb) < chunk) {
            xemacif_input(&echo_netif);  // Process incoming ACK packets
            //usleep(100);                 // Small delay to avoid busy-wait
        }

        // Write chunk into TCP send buffer
        err = tcp_write(client_pcb, &buffer[offset], chunk, TCP_WRITE_FLAG_COPY);
        if (err != ERR_OK) {
            return -2;
        }

        // Flush TCP send buffer immediately
        err = tcp_output(client_pcb);
        if (err != ERR_OK) {
            return -3;
        }

        // Update counters
        offset += chunk;
        remaining -= chunk;
    }

    return 0;
}

/* -------------------------------------------------------------------------- */
/* RX callback: copy into ring, apply backpressure                            */
/* -------------------------------------------------------------------------- */
static err_t recv_callback(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err)
{
   if (!p) {
       if (!tcp_tx_active) {
           tcp_close(tpcb);
           client_pcb = NULL;
       }
       return ERR_OK;
   }

    if (rx_full() && tcp_rx_offset == 0) return ERR_MEM; // stall

    u32 copied = 0;
    struct pbuf *q = p;
    while (q) {
        u8 *src = (u8 *)q->payload;
        u16 len = q->len;
        u32 remain = IN_FRAME_BYTES - tcp_rx_offset;

        if (len <= remain) {
            memcpy(&tcp_rx_buffers[tcp_rx_wr_idx][tcp_rx_offset], src, len);
            tcp_rx_offset += len;
            copied += len;
            q = q->next;
        } else {
            if (remain > 0) {
                memcpy(&tcp_rx_buffers[tcp_rx_wr_idx][tcp_rx_offset], src, remain);
                tcp_rx_offset += remain;
                copied += remain;
                src += remain;
                len -= remain;
            }
            if (tcp_rx_offset == IN_FRAME_BYTES) {
                Xil_DCacheFlushRange((INTPTR)tcp_rx_buffers[tcp_rx_wr_idx], IN_FRAME_BYTES);
                tcp_rx_ready[tcp_rx_wr_idx] = 1;
                tcp_rx_count++;
                tcp_rx_wr_idx = (tcp_rx_wr_idx + 1) % NUM_BUFFERS;
                tcp_rx_offset = 0;
            }
            if (rx_full()) {
                if (copied > 0) tcp_recved(tpcb, copied);
                return ERR_MEM;
            }
            memcpy(&tcp_rx_buffers[tcp_rx_wr_idx][tcp_rx_offset], src, len);
            tcp_rx_offset += len;
            copied += len;
            q = q->next;
        }
        if (tcp_rx_offset == IN_FRAME_BYTES) {
            Xil_DCacheFlushRange((INTPTR)tcp_rx_buffers[tcp_rx_wr_idx], IN_FRAME_BYTES);
            tcp_rx_ready[tcp_rx_wr_idx] = 1;
            tcp_rx_count++;
            tcp_rx_wr_idx = (tcp_rx_wr_idx + 1) % NUM_BUFFERS;
            tcp_rx_offset = 0;
            if (rx_full() && q) {
                if (copied > 0) tcp_recved(tpcb, copied);
                return ERR_MEM;
            }
        }
    }

    if (copied > 0) tcp_recved(tpcb, copied);
    pbuf_free(p);
    return ERR_OK;
}

/* -------------------------------------------------------------------------- */
/* Accept callback                                                            */
/* -------------------------------------------------------------------------- */
static err_t accept_callback(void *arg, struct tcp_pcb *newpcb, err_t err)
{
    client_pcb = newpcb;
    tcp_recv(newpcb, recv_callback);
    tcp_sent(newpcb, send_callback);
    return ERR_OK;
}

/* -------------------------------------------------------------------------- */
/* Start server                                                               */
/* -------------------------------------------------------------------------- */
int start_application(void)
{
    struct tcp_pcb *pcb = tcp_new_ip_type(IPADDR_TYPE_ANY);
    if (!pcb) return -1;

    if (tcp_bind(pcb, IP_ANY_TYPE, TCP_PORT) != ERR_OK) {
        tcp_abort(pcb);
        return -2;
    }

    pcb = tcp_listen(pcb);
    if (!pcb) return -3;

    tcp_accept(pcb, accept_callback);

    return 0;
}
