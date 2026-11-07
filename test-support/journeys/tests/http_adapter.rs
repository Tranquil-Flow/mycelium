// SPDX-License-Identifier: AGPL-3.0-only
//! Real loopback adapter corpus; not product SSE/serving evidence.
use budgets::BudgetId;
use journey_tests::{duration, http::Client};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, TcpListener},
    sync::mpsc,
    thread,
};

fn serve(reply: &'static [u8]) -> (String, thread::JoinHandle<Vec<u8>>) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
    let job = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap()))
            .unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut request = Vec::new();
        let mut length = 0;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            request.extend(line.as_bytes());
            if let Some(v) = line.strip_prefix("Content-Length: ") {
                length = v.trim().parse().unwrap();
            }
            if line == "\r\n" {
                break;
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        request.extend(body);
        // Deliberately fragment framing and UTF-8, independent of TCP packet boundaries.
        for part in reply.chunks(3) {
            stream.write_all(part).unwrap();
        }
        request
    });
    (origin, job)
}
#[test]
fn actual_tcp_chunked_sse_preserves_terminal_order() {
    let (origin,job)=serve(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n17\r\nevent: token\ndata: hi\n\n\r\n16\r\nevent: done\ndata: {}\n\n\r\n0\r\n\r\n");
    let response = Client::new(&origin)
        .unwrap()
        .request("GET", "/events", &[], &[], false, BudgetId::ModelSelection)
        .unwrap();
    let events = response.events().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["event"], "token");
    assert_eq!(events[0]["data"], "hi");
    assert_eq!(events[1]["event"], "done");
    assert!(
        String::from_utf8(job.join().unwrap())
            .unwrap()
            .starts_with("GET /events HTTP/1.1")
    );
}
#[test]
fn truncated_and_ambiguous_responses_fail_not_success() {
    for reply in [
        b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n{}".as_slice(),
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n".as_slice(),
    ] {
        let (origin, job) = serve(reply);
        assert!(
            Client::new(&origin)
                .unwrap()
                .request("GET", "/", &[], &[], false, BudgetId::ModelSelection)
                .is_err()
        );
        assert!(!job.join().unwrap().is_empty());
    }
}
#[test]
fn one_total_deadline_rejects_stalled_response() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
    let (release, wait) = mpsc::channel();
    let job = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        wait.recv().unwrap();
        drop(stream);
    });
    let result =
        Client::new(&origin)
            .unwrap()
            .request("GET", "/", &[], &[], false, BudgetId::OwnerPoll);
    release.send(()).unwrap();
    job.join().unwrap();
    let error = result.err().expect("stalled server passed");
    assert!(
        error.contains("WouldBlock") || error.contains("TimedOut") || error.contains("deadline"),
        "{error}"
    );
}
#[test]
fn session_cookie_is_consumed_and_origin_sent_without_redirect_following() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
    let expected = origin.clone();
    let job = thread::spawn(move || {
        for (index,reply) in ["HTTP/1.1 303 See Other\r\nContent-Length: 0\r\nLocation: http://192.0.2.1/never-follow\r\nSet-Cookie: a13session=canary; HttpOnly; SameSite=Strict; Path=/\r\n\r\n","HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"].into_iter().enumerate() {
            let (mut stream,_)=listener.accept().unwrap();
            stream.set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap())).unwrap();
            let mut reader=BufReader::new(stream.try_clone().unwrap());
            let mut headers=String::new(); let mut length=0;
            loop { let mut line=String::new();reader.read_line(&mut line).unwrap();if let Some(v)=line.strip_prefix("Content-Length: "){length=v.trim().parse().unwrap();}headers.push_str(&line);if line=="\r\n"{break;} }
            let mut body=vec![0;length];reader.read_exact(&mut body).unwrap();
            if index==0 { assert!(headers.contains("Origin: null")); assert_eq!(body,b"launch=one-use-canary"); }
            else { assert!(headers.contains("Cookie: a13session=canary")); assert!(headers.contains(&format!("Origin: {expected}")));assert!(headers.contains("X-Mycelium-Action: 1")); }
            stream.write_all(reply.as_bytes()).unwrap();
        }
    });
    let mut client = Client::new(&origin).unwrap();
    client.sign_in("one-use-canary").unwrap();
    assert_eq!(
        client
            .post("/api/fast-mode", &serde_json::json!({"enabled":true}))
            .unwrap()
            .status,
        200
    );
    job.join().unwrap();
}
