// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_request_gateway/asgi.py — failure/cleanup transport controls.
use api::{
    http::{Reply, Response, StreamingBody, TransportError, serve_response_connection},
    schema::PublicError,
    sse::{EventStream, EventValidator, StreamError},
};
use budgets::BudgetId;
use journey_tests::duration;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    sync::{Arc, Mutex},
    thread,
};
struct Validator;
impl EventValidator for Validator {
    fn validate(&self, _: &Value) -> Result<(), PublicError> {
        Ok(())
    }
}
#[test]
fn resume_binding_rejects_first_wrong_request_and_replayed_cursor() {
    for event in [
        json!({"request_id":"other","publisher_generation":4,"sequence":8,"type":"token"}),
        json!({"request_id":"request","publisher_generation":4,"sequence":7,"type":"token"}),
        json!({"request_id":"request","publisher_generation":5,"sequence":8,"type":"token"}),
    ] {
        let mut stream =
            EventStream::for_request(Validator, vec![], "request".into(), Some((4, 7)));
        let mut bytes = Vec::new();
        assert_eq!(
            stream.write(&mut bytes, event).unwrap_err().code,
            "stream_order_invalid"
        );
        assert!(bytes.is_empty());
    }
    let mut stream = EventStream::for_request(Validator, vec![], "request".into(), None);
    assert!(stream.write(&mut Vec::new(),json!({"request_id":"other","publisher_generation":1,"sequence":1,"type":"accepted"})).is_err());
}
struct Source {
    trace: Arc<Mutex<Vec<&'static str>>>,
    fail_write: bool,
    fail_close: bool,
}
impl StreamingBody for Source {
    fn write(&mut self, _: &mut dyn Write) -> Result<(), StreamError> {
        self.trace.lock().unwrap().push("write");
        if self.fail_write {
            Err(PublicError::new(500, "source_failed").into())
        } else {
            Ok(())
        }
    }
    fn close(&mut self) -> Result<(), PublicError> {
        self.trace.lock().unwrap().push("close");
        if self.fail_close {
            Err(PublicError::new(500, "close_failed"))
        } else {
            Ok(())
        }
    }
}
#[test]
fn source_cleanup_runs_on_bad_headers_head_and_preserves_both_failures() {
    for mode in ["headers", "head", "both"] {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let trace = Arc::new(Mutex::new(Vec::new()));
        let worker_trace = trace.clone();
        let job = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            serve_response_connection(
                stream,
                duration(BudgetId::ModelSelection).unwrap(),
                65536,
                |_| Ok(None),
                |_| {
                    Ok(Response::Stream {
                        reply: Reply {
                            status: 200,
                            headers: if mode == "headers" {
                                vec![("bad\r\n".into(), "v".into())]
                            } else {
                                vec![]
                            },
                            body: vec![],
                        },
                        body: Box::new(Source {
                            trace: worker_trace,
                            fail_write: mode == "both",
                            fail_close: mode == "both",
                        }),
                    })
                },
            )
        });
        let mut client = TcpStream::connect(addr).unwrap();
        client
            .set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap()))
            .unwrap();
        write!(
            client,
            "{} / HTTP/1.1\r\nHost: {addr}\r\n\r\n",
            if mode == "head" { "HEAD" } else { "GET" }
        )
        .unwrap();
        let mut bytes = Vec::new();
        client.read_to_end(&mut bytes).unwrap();
        drop(client);
        let outcome = job.join().unwrap();
        if mode == "both" {
            match outcome {
                Err(TransportError::Cleanup {
                    primary: Some(primary),
                    cleanup,
                }) => {
                    assert_eq!(cleanup.code, "close_failed");
                    assert!(matches!(
                        *primary,
                        TransportError::Stream(StreamError {
                            code: "source_failed",
                            ..
                        })
                    ));
                }
                other => panic!("{other:?}"),
            };
            assert_eq!(*trace.lock().unwrap(), vec!["write", "close"]);
        } else {
            assert_eq!(*trace.lock().unwrap(), vec!["close"]);
            if mode == "head" {
                outcome.unwrap();
            } else {
                assert!(matches!(
                    outcome,
                    Err(TransportError::Boundary(PublicError {
                        code: "response_header_invalid",
                        ..
                    }))
                ));
            }
        }
    }
}
