// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded HTTP/1.x completion detection shared by directory, pairing and seed clients.
//! Unframed/malformed replies retain the legacy read-to-EOF path. Checked callers
//! can refuse hostile arithmetic and excessive interim responses immediately.

/// Maximum consecutive 100 Continue heads before a final response.
pub const INTERIM_RESPONSE_LIMIT: usize = 32;

/// Compatibility probe for clients with their own decoding/error mapping.
#[must_use]
pub fn response_complete(reply: &[u8]) -> bool {
    match response_complete_checked(reply) {
        Ok(complete) => complete,
        Err(code) => {
            eprintln!("{code}");
            false
        }
    }
}

/// Find the final head without recursion. As in http.client, only 100 is skipped;
/// other 1xx statuses are bodyless final replies (including protocol switching).
pub fn final_response(mut reply: &[u8]) -> Result<Option<&[u8]>, &'static str> {
    let mut interim = 0;
    loop {
        let Some(split) = reply.windows(4).position(|w| w == b"\r\n\r\n") else {
            return Ok(None);
        };
        let Ok(head) = std::str::from_utf8(&reply[..split]) else {
            return Ok(None);
        };
        let Some(status) = head
            .lines()
            .next()
            .and_then(|l| l.split(' ').nth(1))
            .and_then(|s| s.parse::<u16>().ok())
        else {
            return Ok(None);
        };
        if status != 100 {
            return Ok(Some(reply));
        }
        if head.split("\r\n").skip(1).any(|line| !line.contains(':')) {
            return Ok(None);
        }
        interim += 1;
        if interim > INTERIM_RESPONSE_LIMIT {
            return Err("http_interim_response_limit");
        }
        reply = &reply[split + 4..];
    }
}

/// Complete, incomplete, or a specific framing refusal (never an arithmetic panic).
pub fn response_complete_checked(reply: &[u8]) -> Result<bool, &'static str> {
    let Some(reply) = final_response(reply)? else {
        return Ok(false);
    };
    let Some(split) = reply.windows(4).position(|w| w == b"\r\n\r\n") else {
        return Ok(false);
    };
    let Ok(head) = std::str::from_utf8(&reply[..split]) else {
        return Ok(false);
    };
    let body = &reply[split + 4..];
    let mut lines = head.split("\r\n");
    let Some(status) = lines
        .next()
        .and_then(|l| l.split(' ').nth(1))
        .and_then(|s| s.parse::<u16>().ok())
    else {
        return Ok(false);
    };
    let (mut chunked, mut length, mut length_lines) = (false, None, 0usize);
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return Ok(false);
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
    if status == 204 || status == 304 || (100..200).contains(&status) {
        return Ok(true);
    }
    if chunked {
        return chunks_complete(body);
    }
    Ok(match (length_lines, length) {
        (1, Some(length)) => body.len() >= length,
        _ => false,
    })
}

fn chunks_complete(mut rest: &[u8]) -> Result<bool, &'static str> {
    loop {
        let Some(line_end) = rest.windows(2).position(|w| w == b"\r\n") else {
            return Ok(false);
        };
        let Some(size) = std::str::from_utf8(&rest[..line_end])
            .ok()
            .and_then(|l| l.split(';').next())
            .and_then(|l| usize::from_str_radix(l.trim(), 16).ok())
        else {
            return Ok(false);
        };
        rest = &rest[line_end + 2..];
        if size == 0 {
            return Ok(rest.starts_with(b"\r\n") || rest.windows(4).any(|w| w == b"\r\n\r\n"));
        }
        let framed_size = size.checked_add(2).ok_or("http_chunk_size_overflow")?;
        if rest.len() < framed_size {
            return Ok(false);
        }
        rest = &rest[framed_size..];
    }
}

#[cfg(test)]
mod tests {
    use super::response_complete as complete;

    #[test]
    fn checked_framing_refuses_arithmetic_and_interim_limits() {
        use super::{INTERIM_RESPONSE_LIMIT, response_complete_checked};
        let prefix = b"HTTP/1.1 100 Continue\r\n\r\n";
        let mut at_limit = prefix.repeat(INTERIM_RESPONSE_LIMIT);
        assert_eq!(response_complete_checked(&at_limit), Ok(false));
        at_limit.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}");
        assert_eq!(response_complete_checked(&at_limit), Ok(true));
        assert_eq!(
            response_complete_checked(&prefix.repeat(INTERIM_RESPONSE_LIMIT + 1)),
            Err("http_interim_response_limit"),
        );
        for size in [usize::MAX, usize::MAX - 1] {
            let reply =
                format!("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{size:x}\r\nx");
            assert_eq!(
                response_complete_checked(reply.as_bytes()),
                Err("http_chunk_size_overflow")
            );
            assert!(!complete(reply.as_bytes()));
        }
        let reply = format!(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{:x}\r\nx",
            usize::MAX - 2
        );
        assert_eq!(response_complete_checked(reply.as_bytes()), Ok(false));
    }

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
