from rulfo._core import ffi, lib


def test_the_module_has_the_defaults_of_the_core() -> None:
    config = ffi.new("rf_config *")
    lib.rf_config_default(config)
    assert config.max_length == lib.RF_DEFAULT_MAX_LENGTH == 16 * 1024 * 1024
    assert (config.max_out, config.max_requests, config.max_ids) == (1024, 64, 4096)
    assert (config.handshake_ms, config.keep_ms, config.max_deadline_ms) == (5000, 60000, 3600000)


def test_a_frame_goes_through_the_codec() -> None:
    key, body = ffi.new("uint8_t[]", b"key"), ffi.new("uint8_t[]", b"body")
    frame = ffi.new("rf_frame *", {"kind": lib.RF_REQUEST, "argument": 7})
    frame.id.bytes = bytes(range(16))
    frame.key.ptr, frame.key.len = key, 3
    frame.body.ptr, frame.body.len = body, 4
    out, size = ffi.new("uint8_t[64]"), ffi.new("size_t *")
    assert lib.rf_frame_encode(frame, 1024, out, 64, size) == lib.RF_OK
    assert size[0] == lib.RF_HEADER_SIZE + 7

    read = ffi.new("rf_frame *")
    assert lib.rf_frame_decode(out, size[0], 1024, read, size) == lib.RF_OK
    assert (read.argument, bytes(read.id.bytes)) == (7, bytes(range(16)))
    assert bytes(ffi.buffer(read.key.ptr, read.key.len)) == b"key"
    assert bytes(ffi.buffer(read.body.ptr, read.body.len)) == b"body"
