#pragma once

#include <cstddef>
#include <cstdint>

class pwdEncode
{
public:
    pwdEncode();
    ~pwdEncode();
public:
    // 解密：密文二进制、密文长度、32字节密钥，返回明文堆char*，需free
    char* aes256_decrypt(const unsigned char* ciphertext, size_t ciphertext_len, const char* key);

    // 加密：明文字符串、密钥、输出密文长度，返回密文堆unsigned char*，需free
    unsigned char* aes256_encrypt(const char* plaintext, const char* key, size_t* ciphertext_len);
    unsigned char* base64_decode(const char* encoded, size_t* out_len);
    char* base64_encode(const uint8_t* data, size_t data_len, size_t* out_len);
    char* decrypt_string(const char* encrypted_base64, const char* key);
    char* encrypt_string(const char* data, const char* key);
private:
    // ---------------- AES 底层原生实现（无第三方库）----------------
    static void aes_key_expand(const uint8_t* key, uint32_t* w);
    static void aes_sub_bytes(uint8_t* state);
    static void aes_inv_sub_bytes(uint8_t* state);
    static void aes_shift_rows(uint8_t* state);
    static void aes_inv_shift_rows(uint8_t* state);
    static void aes_mix_columns(uint8_t* state);
    static void aes_inv_mix_columns(uint8_t* state);
    static void aes_add_round_key(uint8_t* state, const uint32_t* w, int round);
    static void aes_encrypt_block(uint8_t* block, const uint32_t* w);
    static void aes_decrypt_block(uint8_t* block, const uint32_t* w);

    // CBC工具函数
    static void pkcs7_pad(uint8_t* data, size_t len, size_t block_size, size_t* out_len);
    static size_t pkcs7_unpad(const uint8_t* data, size_t len);
    static void xor_block(uint8_t* dst, const uint8_t* a, const uint8_t* b, size_t block_size);

    // 常量S盒/逆S盒/Rcon
    static const uint8_t sbox[256];
    static const uint8_t inv_sbox[256];
    static const uint32_t rcon[15];
    static const char b64_table[65];
    static const uint8_t b64_rev_table[256];
};
