#include "pwdEncode.h"



#include <cstring>
#include <cstdlib>
#include <ctime>
#include <cctype>

// ===================== AES 固定常量 =====================
const uint8_t pwdEncode::sbox[256] = {
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
};

const uint8_t pwdEncode::inv_sbox[256] = {
    0x52,0x09,0x6a,0xd5,0x30,0x36,0xa5,0x38,0xbf,0x40,0xa3,0x9e,0x81,0xf3,0xd7,0xfb,
    0x7c,0xe3,0x39,0x82,0x9b,0x2f,0xff,0x87,0x34,0x8e,0x43,0x44,0xc4,0xde,0xe9,0xcb,
    0x54,0x7b,0x94,0x32,0xa6,0xc2,0x23,0x3d,0xee,0x4c,0x95,0x0b,0x42,0xfa,0xc3,0x4e,
    0x08,0x2e,0xa1,0x66,0x28,0xd9,0x24,0xb2,0x76,0x5b,0xa2,0x49,0x6d,0x8b,0xd1,0x25,
    0x72,0xf8,0xf6,0x64,0x86,0x68,0x98,0x16,0xd4,0xa4,0x5c,0xcc,0x5d,0x65,0xb6,0x92,
    0x6c,0x70,0x48,0x50,0xfd,0xed,0xb9,0xda,0x5e,0x15,0x46,0x57,0xa7,0x8d,0x9d,0x84,
    0x90,0xd8,0xab,0x00,0x8c,0xbc,0xd3,0x0a,0xf7,0xe4,0x58,0x05,0xb8,0xb3,0x45,0x06,
    0xd0,0x2c,0x1e,0x8f,0xca,0x3f,0x0f,0x02,0xc1,0xaf,0xbd,0x03,0x01,0x13,0x8a,0x6b,
    0x3a,0x91,0x11,0x41,0x4f,0x67,0xdc,0xea,0x97,0xf2,0xcf,0xce,0xf0,0xb4,0xe6,0x73,
    0x96,0xac,0x74,0x22,0xe7,0xad,0x35,0x85,0xe2,0xf9,0x37,0xe8,0x1c,0x75,0xdf,0x6e,
    0x47,0xf1,0x1a,0x71,0x1d,0x29,0xc5,0x89,0x6f,0xb7,0x62,0x0e,0xaa,0x18,0xbe,0x1b,
    0xfc,0x56,0x3e,0x4b,0xc6,0xd2,0x79,0x20,0x9a,0xdb,0xc0,0xfe,0x78,0xcd,0x5a,0xf4,
    0x1f,0xdd,0xa8,0x33,0x88,0x07,0xc7,0x31,0xb1,0x12,0x10,0x59,0x27,0x80,0xec,0x5f,
    0x60,0x51,0x7f,0xa9,0x19,0xb5,0x4a,0x0d,0x2d,0xe5,0x7a,0x9f,0x93,0xc9,0x9c,0xef,
    0xa0,0xe0,0x3b,0x4d,0xae,0x2a,0xf5,0xb0,0xc8,0xeb,0xbb,0x3c,0x83,0x53,0x99,0x61,
    0x17,0x2b,0x04,0x7e,0xba,0x77,0xd6,0x26,0xe1,0x69,0x14,0x63,0x55,0x21,0x0c,0x7d
};

const uint32_t pwdEncode::rcon[15] = {
    0x01000000,0x02000000,0x04000000,0x08000000,0x10000000,
    0x20000000,0x40000000,0x80000000,0x1B000000,0x36000000,
    0x6C000000,0xD8000000,0xAB000000,0x4D000000,0x9A000000
};

// ===================== Base64 常量表 =====================
const char pwdEncode::b64_table[65] =
"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

const uint8_t pwdEncode::b64_rev_table[256] = {
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,62,64,64,64,63,
    52,53,54,55,56,57,58,59,60,61,64,64,64,64,64,64,
    64, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,
    15,16,17,18,19,20,21,22,23,24,25,64,64,64,64,64,
    64,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,
    41,42,43,44,45,46,47,48,49,50,51,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64
};

// ===================== 公共工具函数 =====================

pwdEncode::pwdEncode()
{

}

pwdEncode::~pwdEncode()
{

}

static uint8_t gmul(uint8_t a, uint8_t b)
{
    uint8_t res = 0;
    for (int i = 0; i < 8; i++)
    {
        if (b & 1) res ^= a;
        uint8_t hi = a & 0x80;
        a <<= 1;
        if (hi) a ^= 0x1b;
        b >>= 1;
    }
    return res;
}

void pwdEncode::xor_block(uint8_t* dst, const uint8_t* a, const uint8_t* b, size_t block_size)
{
    for (size_t i = 0; i < block_size; i++)
        dst[i] = a[i] ^ b[i];
}

void pwdEncode::pkcs7_pad(uint8_t* data, size_t len, size_t block_size, size_t* out_len)
{
    size_t pad = block_size - (len % block_size);
    *out_len = len + pad;
    if (data != nullptr)
    {
        memset(data + len, pad, pad);
    }
}

size_t pwdEncode::pkcs7_unpad(const uint8_t* data, size_t len)
{
    if (len == 0) return 0;
    uint8_t pad = data[len - 1];
    if (pad > len || pad == 0) return len;
    for (size_t i = len - pad; i < len; i++)
        if (data[i] != pad) return len;
    return len - pad;
}

// ===================== AES 底层轮函数 =====================
void pwdEncode::aes_sub_bytes(uint8_t* state)
{
    for (int i = 0; i < 16; i++) state[i] = sbox[state[i]];
}

void pwdEncode::aes_inv_sub_bytes(uint8_t* state)
{
    for (int i = 0; i < 16; i++) state[i] = inv_sbox[state[i]];
}

void pwdEncode::aes_shift_rows(uint8_t* s)
{
    uint8_t t;
    t = s[1]; s[1] = s[5]; s[5] = s[9]; s[9] = s[13]; s[13] = t;
    t = s[2]; s[2] = s[10]; s[10] = t;
    t = s[6]; s[6] = s[14]; s[14] = t;
    t = s[15]; s[15] = s[11]; s[11] = s[7]; s[7] = s[3]; s[3] = t;
}

void pwdEncode::aes_inv_shift_rows(uint8_t* s)
{
    uint8_t t;
    t = s[13]; s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;
    t = s[2]; s[2] = s[10]; s[10] = t;
    t = s[6]; s[6] = s[14]; s[14] = t;
    t = s[3]; s[3] = s[7]; s[7] = s[11]; s[11] = s[15]; s[15] = t;
}

void pwdEncode::aes_mix_columns(uint8_t* s)
{
    for (int c = 0; c < 4; c++)
    {
        uint8_t a0 = s[c * 4 + 0], a1 = s[c * 4 + 1], a2 = s[c * 4 + 2], a3 = s[c * 4 + 3];
        s[c * 4 + 0] = gmul(0x02, a0) ^ gmul(0x03, a1) ^ a2 ^ a3;
        s[c * 4 + 1] = a0 ^ gmul(0x02, a1) ^ gmul(0x03, a2) ^ a3;
        s[c * 4 + 2] = a0 ^ a1 ^ gmul(0x02, a2) ^ gmul(0x03, a3);
        s[c * 4 + 3] = gmul(0x03, a0) ^ a1 ^ a2 ^ gmul(0x02, a3);
    }
}

void pwdEncode::aes_inv_mix_columns(uint8_t* s)
{
    for (int c = 0; c < 4; c++)
    {
        uint8_t a0 = s[c * 4 + 0], a1 = s[c * 4 + 1], a2 = s[c * 4 + 2], a3 = s[c * 4 + 3];
        s[c * 4 + 0] = gmul(0x0e, a0) ^ gmul(0x0b, a1) ^ gmul(0x0d, a2) ^ gmul(0x09, a3);
        s[c * 4 + 1] = gmul(0x09, a0) ^ gmul(0x0e, a1) ^ gmul(0x0b, a2) ^ gmul(0x0d, a3);
        s[c * 4 + 2] = gmul(0x0d, a0) ^ gmul(0x09, a1) ^ gmul(0x0e, a2) ^ gmul(0x0b, a3);
        s[c * 4 + 3] = gmul(0x0b, a0) ^ gmul(0x0d, a1) ^ gmul(0x09, a2) ^ gmul(0x0e, a3);
    }
}

void pwdEncode::aes_add_round_key(uint8_t* state, const uint32_t* w, int round)
{
    for (int i = 0; i < 4; i++)
    {
        uint32_t rk = w[round * 4 + i];
        state[i * 4 + 0] ^= (rk >> 24) & 0xff;
        state[i * 4 + 1] ^= (rk >> 16) & 0xff;
        state[i * 4 + 2] ^= (rk >> 8) & 0xff;
        state[i * 4 + 3] ^= rk & 0xff;
    }
}

void pwdEncode::aes_key_expand(const uint8_t* key, uint32_t* w)
{
    for (int i = 0; i < 8; i++)
        w[i] = (key[i * 4] << 24) | (key[i * 4 + 1] << 16) | (key[i * 4 + 2] << 8) | key[i * 4 + 3];

    for (int i = 8; i < 60; i++)
    {
        uint32_t temp = w[i - 1];
        if (i % 8 == 0)
        {
            temp = (temp << 8) | ((temp >> 24) & 0xff);
            temp = (sbox[(temp >> 24) & 0xff] << 24) | (sbox[(temp >> 16) & 0xff] << 16)
                | (sbox[(temp >> 8) & 0xff] << 8) | sbox[temp & 0xff];
            temp ^= rcon[i / 8 - 1];
        }
        else if (i % 8 == 4)
        {
            temp = (sbox[(temp >> 24) & 0xff] << 24) | (sbox[(temp >> 16) & 0xff] << 16)
                | (sbox[(temp >> 8) & 0xff] << 8) | sbox[temp & 0xff];
        }
        w[i] = w[i - 8] ^ temp;
    }
}

void pwdEncode::aes_encrypt_block(uint8_t* block, const uint32_t* w)
{
    aes_add_round_key(block, w, 0);
    for (int r = 1; r <= 13; r++)
    {
        aes_sub_bytes(block);
        aes_shift_rows(block);
        aes_mix_columns(block);
        aes_add_round_key(block, w, r);
    }
    aes_sub_bytes(block);
    aes_shift_rows(block);
    aes_add_round_key(block, w, 14);
}

void pwdEncode::aes_decrypt_block(uint8_t* block, const uint32_t* w)
{
    aes_add_round_key(block, w, 14);
    aes_inv_shift_rows(block);
    aes_inv_sub_bytes(block);
    for (int r = 13; r >= 1; r--)
    {
        aes_add_round_key(block, w, r);
        aes_inv_mix_columns(block);
        aes_inv_shift_rows(block);
        aes_inv_sub_bytes(block);
    }
    aes_add_round_key(block, w, 0);
}

// ===================== Base64 底层实现 =====================
char* pwdEncode::base64_encode(const uint8_t* data, size_t data_len, size_t* out_len)
{
    if (!data || out_len == nullptr) return nullptr;
    size_t enc_len = ((data_len + 2) / 3) * 4;
    char* buf = (char*)malloc(enc_len + 1);
    if (!buf) return nullptr;
    size_t idx = 0;
    size_t i = 0;
    for (; i + 2 < data_len; i += 3)
    {
        uint32_t val = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2];
        buf[idx++] = b64_table[(val >> 18) & 0x3F];
        buf[idx++] = b64_table[(val >> 12) & 0x3F];
        buf[idx++] = b64_table[(val >> 6) & 0x3F];
        buf[idx++] = b64_table[val & 0x3F];
    }
    if (i < data_len)
    {
        uint32_t val = data[i] << 16;
        if (i + 1 < data_len) val |= data[i + 1] << 8;
        buf[idx++] = b64_table[(val >> 18) & 0x3F];
        buf[idx++] = b64_table[(val >> 12) & 0x3F];
        if (i + 1 < data_len)
            buf[idx++] = b64_table[(val >> 6) & 0x3F];
        else
            buf[idx++] = '=';
        buf[idx++] = '=';
    }
    buf[enc_len] = '\0';
    *out_len = enc_len;
    return buf;
}

unsigned char* pwdEncode::base64_decode(const char* encoded, size_t* out_len)
{
    if (!encoded || !out_len) return nullptr;
    size_t str_len = strlen(encoded);
    size_t pad_cnt = 0;
    if (str_len >= 1 && encoded[str_len - 1] == '=') pad_cnt++;
    if (str_len >= 2 && encoded[str_len - 2] == '=') pad_cnt++;
    size_t raw_len = (str_len / 4) * 3 - pad_cnt;
    uint8_t* raw = (uint8_t*)malloc(raw_len);
    if (!raw) return nullptr;
    size_t raw_idx = 0;
    uint32_t accum = 0;
    int bits = 0;
    for (size_t i = 0; i < str_len; i++)
    {
        char c = encoded[i];
        if (c == '=') break;
        uint8_t val = b64_rev_table[(uint8_t)c];
        if (val == 64) continue; // 跳过非法字符
        accum = (accum << 6) | val;
        bits += 6;
        if (bits >= 8)
        {
            bits -= 8;
            raw[raw_idx++] = (accum >> bits) & 0xFF;
        }
    }
    *out_len = raw_idx;
    return raw;
}

// ===================== 原始二进制AES接口实现 =====================
unsigned char* pwdEncode::aes256_encrypt(const char* plaintext, const char* key, size_t* ciphertext_len)
{
    if (!plaintext || !key || !ciphertext_len) return nullptr;
    size_t text_len = strlen(plaintext);
    const size_t BLOCK = 16;
    const size_t KEY_LEN = 32;

    uint8_t aes_key[KEY_LEN] = { 0 };
    size_t klen = strlen(key);
    memcpy(aes_key, key, (klen > KEY_LEN) ? KEY_LEN : klen);

    uint32_t w[60];
    aes_key_expand(aes_key, w);

    size_t padded_len;
    pkcs7_pad(nullptr, text_len, BLOCK, &padded_len);
    size_t total_out = BLOCK + padded_len;
    unsigned char* out = (unsigned char*)malloc(total_out);
    if (!out) return nullptr;

    srand((unsigned)time(nullptr));
    for (size_t i = 0; i < BLOCK; i++)
        out[i] = rand() & 0xff;
    uint8_t iv[BLOCK];
    memcpy(iv, out, BLOCK);

    uint8_t* buf = out + BLOCK;
    memcpy(buf, plaintext, text_len);
    pkcs7_pad(buf, text_len, BLOCK, &padded_len);

    uint8_t prev[BLOCK];
    memcpy(prev, iv, BLOCK);
    for (size_t off = 0; off < padded_len; off += BLOCK)
    {
        uint8_t block[BLOCK];
        xor_block(block, buf + off, prev, BLOCK);
        aes_encrypt_block(block, w);
        memcpy(buf + off, block, BLOCK);
        memcpy(prev, block, BLOCK);
    }

    *ciphertext_len = total_out;
    return out;
}

char* pwdEncode::aes256_decrypt(const unsigned char* ciphertext, size_t ciphertext_len, const char* key)
{
    if (!ciphertext || ciphertext_len <= 16 || !key) return nullptr;
    const size_t BLOCK = 16;
    const size_t KEY_LEN = 32;

    uint8_t iv[BLOCK];
    memcpy(iv, ciphertext, BLOCK);
    const uint8_t* enc_data = ciphertext + BLOCK;
    size_t enc_len = ciphertext_len - BLOCK;

    uint8_t aes_key[KEY_LEN] = { 0 };
    size_t klen = strlen(key);
    memcpy(aes_key, key, (klen > KEY_LEN) ? KEY_LEN : klen);
    uint32_t w[60];
    aes_key_expand(aes_key, w);

    uint8_t* dec_buf = (uint8_t*)malloc(enc_len);
    if (!dec_buf) return nullptr;
    memcpy(dec_buf, enc_data, enc_len);

    uint8_t prev[BLOCK];
    memcpy(prev, iv, BLOCK);
    for (size_t off = 0; off < enc_len; off += BLOCK)
    {
        uint8_t block[BLOCK];
        memcpy(block, dec_buf + off, BLOCK);
        aes_decrypt_block(block, w);
        xor_block(dec_buf + off, block, prev, BLOCK);
        memcpy(prev, enc_data + off, BLOCK);
    }

    size_t plain_len = pkcs7_unpad(dec_buf, enc_len);
    char* plain = (char*)malloc(plain_len + 1);
    memcpy(plain, dec_buf, plain_len);
    plain[plain_len] = '\0';

    free(dec_buf);
    return plain;
}

// ===================== 新增：AES+Base64 封装接口实现 =====================
char* pwdEncode::encrypt_string(const char* data, const char* key)
{
    if (!data || !key) return nullptr;
    size_t bin_cipher_len = 0;
    unsigned char* bin_cipher = aes256_encrypt(data, key, &bin_cipher_len);
    if (!bin_cipher) return nullptr;

    size_t b64_len = 0;
    char* b64_str = base64_encode(bin_cipher, bin_cipher_len, &b64_len);
    free(bin_cipher);
    return b64_str;
}

char* pwdEncode::decrypt_string(const char* encrypted_base64, const char* key)
{
    if (!encrypted_base64 || !key) return nullptr;
    size_t bin_len = 0;
    uint8_t* bin_data = base64_decode(encrypted_base64, &bin_len);
    if (!bin_data || bin_len <= 16)
    {
        free(bin_data);
        return nullptr;
    }
    char* plain = aes256_decrypt(bin_data, bin_len, key);
    free(bin_data);
    return plain;
}