// SPDX-License-Identifier: AGPL-3.0-only
// Test-only real-file backend for the artifact service trait. It exercises
// commit-before-read and duplicate refusal, not source journal restart/pruning.
struct FixtureObjects {
    root:std::path::PathBuf,
    state:std::sync::Mutex<(std::collections::BTreeSet<String>,u64,usize)>,
}
impl FixtureObjects {
    fn new()->Self {
        let root=std::env::temp_dir().join(format!("l9-artifacts-{}",std::process::id()));
        std::fs::create_dir(&root).unwrap();
        #[cfg(unix)] {use std::os::unix::fs::PermissionsExt;std::fs::set_permissions(&root,std::fs::Permissions::from_mode(0o700)).unwrap();}
        std::fs::write(root.join("object"),artifact_data()).unwrap();
        Self{root,state:std::sync::Mutex::new((Default::default(),0,0))}
    }
    fn read_count(&self)->usize{self.state.lock().unwrap().2}
    fn cleanup(&self){
        for name in ["object","journal"] {std::fs::remove_file(self.root.join(name)).unwrap();}
        std::fs::remove_dir(&self.root).unwrap();
    }
}
impl runtime::iroh_artifact_source::ArtifactObjects for FixtureObjects {
    fn verified_size(&self,digest:&str)->Option<u64> {
        let data=std::fs::read(self.root.join("object")).unwrap();
        (ag::range_digest(&data)==digest).then_some(data.len() as u64)
    }
    fn admit_and_read(&self,grant:&ag::Grant,request:&ag::ChunkRequest,nonce:&str,_expiry:u64,_now:u64)->Result<runtime::iroh_artifact_source::AdmittedChunk,runtime::iroh_artifact::ArtifactError> {
        use runtime::iroh_artifact::{ArtifactError};
        if request.request_id=="held" {
            println!("L9_ARTIFACT_HELD true");std::io::stdout().flush().unwrap();
            let mut line=String::new();std::io::stdin().read_line(&mut line).unwrap();assert_eq!(line.trim(),"release");
        }
        let fence=format!("{}\0{}\0{}",grant.grant_id,request.request_id,nonce);
        let mut state=self.state.lock().unwrap();
        if state.0.contains(&fence){return Err(ArtifactError("artifact_chunk_request_replay".into()));}
        if state.1+request.length_bytes>grant.maximum_total_bytes{return Err(ArtifactError("artifact_grant_budget_exhausted".into()));}
        let mut journal=std::fs::OpenOptions::new().create(true).append(true).open(self.root.join("journal")).unwrap();
        writeln!(journal,"{}",ag::range_digest(fence.as_bytes())).unwrap();journal.sync_data().unwrap();
        state.0.insert(fence);state.1+=request.length_bytes;
        // Commit is on disk before the payload is read. Receipt is signed by
        // the actual service only after this fallible callback returns.
        let data=std::fs::read(self.root.join("object")).unwrap();state.2+=1;
        Ok(runtime::iroh_artifact_source::AdmittedChunk{bytes:data,pace:budgets::Span::ZERO})
    }
}
