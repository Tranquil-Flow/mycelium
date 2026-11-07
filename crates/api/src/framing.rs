// SPDX-License-Identifier: AGPL-3.0-only
//! When has an HTTP/1.x reply arrived in full? `http.client` (the oracle's transport for the pairing
//! window and the seed) returns as soon as the framing says so: `Content-Length` bytes, the final
//! chunk, or no body at all (1xx, 204, 304). It never waits for the peer to hang up, so a relay edge
//! that keeps the connection open after `Connection: close` costs v0.1.4 nothing. The Rust clients read
//! until the peer closes OR this says the reply is complete; anything this cannot frame (no length, a
//! malformed head, an invalid length) stays on the read-to-EOF path with its existing error mapping.

/// `true` only when `reply` holds a whole response by its own framing.
#[must_use]
pub fn response_complete(reply: &[u8]) -> bool {
    let Some(split) = reply.windows(4).position(|w| w == b"\r\n\r\n") else {
        return false;
    };
    let Ok(head) = std::str::from_utf8(&reply[..split]) else {
        return false;
    };
    let body = &reply[split + 4..];
    let mut lines = head.split("\r\n");
    let Some(status) = lines
        .next()
        .and_then(|l| l.split(' ').nth(1))
        .and_then(|s| s.parse::<u16>().ok())
    else {
        return false;
    };
    let (mut chunked, mut length, mut length_lines) = (false, None, 0usize);
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return false;
        };
        match name.trim().to_ascii_lowercase().as_str() {
            "transfer-encoding" => chunked |= value.trim().eq_ignore_ascii_case("chunked"),
            "content-length" => {
                length_lines += 1;
                length = value.trim().parse::<usize>().ok();
            }
            _ => {}
        }
    }
    // `http.client` skips "100 Continue" interim responses and reads the real one after them.
    if status == 100 {
        return response_complete(body);
    }
    if status == 204 || status == 304 || (100..200).contains(&status) {
        return true;
    }
    if chunked {
        return chunks_complete(body);
    }
    // Two Content-Length lines, or one that is not a plain count: leave it to the caller's own rules.
    match (length_lines, length) {
        (1, Some(length)) => body.len() >= length,
        _ => false,
    }
}

/// The chunk sequence ends with a zero-size chunk, optional trailers and the closing blank line.
fn chunks_complete(mut rest: &[u8]) -> bool {
    loop {
        let Some(line_end) = rest.windows(2).position(|w| w == b"\r\n") else {
            return false;
        };
        let Some(size) = std::str::from_utf8(&rest[..line_end])
            .ok()
            .and_then(|l| l.split(';').next())
            .and_then(|l| usize::from_str_radix(l.trim(), 16).ok())
        else {
            return false;
        };
        rest = &rest[line_end + 2..];
        if size == 0 {
            return rest.starts_with(b"\r\n") || rest.windows(4).any(|w| w == b"\r\n\r\n");
        }
        if rest.len() < size + 2 {
            return false;
        }
        rest = &rest[size + 2..];
    }
}

#[cfg(test)]
mod tests {
    use super::response_complete as complete;

    #[test]
    fn content_length_replies() {
        assert!(!complete(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n"));
        assert!(!complete(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nab"));
        assert!(complete(
            b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nabcd"
        ));
        assert!(complete(
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nabcd"
        ));
        assert!(!complete(
            b"HTTP/1.1 200 OK\r\nContent-Length: x\r\n\r\nabcd"
        ));
        assert!(!complete(
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\na"
        ));
        assert!(!complete(b"HTTP/1.1 200 OK\r\n\r\nno framing at all"));
    }

    #[test]
    fn bodyless_statuses() {
        assert!(complete(b"HTTP/1.1 204 No Content\r\n\r\n"));
        assert!(complete(
            b"HTTP/1.1 304 Not Modified\r\nContent-Length: 9\r\n\r\n"
        ));
        assert!(!complete(b"HTTP/1.1 200 OK\r\nContent-Length: 0"));
        assert!(complete(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"));
    }

    #[test]
    fn a_100_continue_is_skipped_like_http_client() {
        assert!(!complete(b"HTTP/1.1 100 Continue\r\n\r\n"));
        assert!(!complete(
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{"
        ));
        assert!(complete(
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
        ));
        // any other 1xx is final with no body (http.client: length = 0)
        assert!(complete(b"HTTP/1.1 103 Early Hints\r\nLink: <x>\r\n\r\n"));
    }

    #[test]
    fn chunked_replies() {
        let head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
        let with = |tail: &[u8]| [head.as_slice(), tail].concat();
        assert!(!complete(&with(b"")));
        assert!(!complete(&with(b"3\r\nabc\r\n")));
        assert!(!complete(&with(b"3\r\nabc\r\n0\r\n")));
        assert!(complete(&with(b"3\r\nabc\r\n0\r\n\r\n")));
        assert!(complete(&with(b"3;ext=1\r\nabc\r\n0\r\nX-T: 1\r\n\r\n")));
        assert!(!complete(&with(b"zz\r\nabc\r\n0\r\n\r\n")));
        // chunked wins over a Content-Length, as in http.client
        let both = b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n";
        assert!(!complete(both));
    }
}
