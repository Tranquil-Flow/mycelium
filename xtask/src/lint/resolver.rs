// SPDX-License-Identifier: AGPL-3.0-only
//! Syntax-level paths, not rustc type inference. Never union unrelated crates.
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use syn::{
    FnArg, Pat, Type,
    visit::{self, Visit},
};

type Positions = BTreeSet<usize>;
#[derive(Clone, Debug)]
pub(super) struct Scope {
    pub root: String,
    pub path: String,
}
impl Scope {
    pub fn source(path: &str) -> Self {
        let parts: Vec<_> = path.trim_end_matches(".rs").split('/').collect();
        if let Some(at) = parts.iter().position(|p| *p == "src") {
            let root = parts[..at].last().unwrap_or(&"workspace").replace('-', "_");
            let mut modules = parts[at + 1..].to_vec();
            if matches!(modules.last(), Some(&"lib" | &"main" | &"mod")) {
                modules.pop();
            }
            return Self {
                path: std::iter::once(root.as_str())
                    .chain(modules)
                    .collect::<Vec<_>>()
                    .join("::"),
                root,
            };
        }
        if let Some(at) = parts
            .iter()
            .position(|p| matches!(*p, "tests" | "examples" | "benches"))
        {
            let root = format!(
                "{}#{}#{}",
                parts[..at].last().unwrap_or(&"workspace").replace('-', "_"),
                parts[at],
                parts.get(at + 1).unwrap_or(&"root")
            );
            return Self {
                path: root.clone(),
                root,
            };
        }
        let root = parts[..parts.len().saturating_sub(1)].join("_");
        Self {
            path: format!("{root}::{}", parts.last().unwrap_or(&"root")),
            root,
        }
    }
    pub fn child(&self, name: &str) -> Self {
        Self {
            root: self.root.clone(),
            path: format!("{}::{name}", self.path),
        }
    }
    pub fn block(&self, block: &syn::Block) -> Self {
        let at = block.brace_token.span.open().start();
        self.child(&format!("#block_{}_{}", at.line, at.column))
    }
    fn namespace(&self) -> &str {
        self.path.split("::#").next().unwrap_or(&self.path)
    }
    fn parents(&self) -> Vec<String> {
        let mut path = self.path.clone();
        let mut scopes = vec![path.clone()];
        while path != self.root {
            let Some((parent, _)) = path.rsplit_once("::") else {
                break;
            };
            path = parent.to_owned();
            scopes.push(path.clone());
        }
        scopes
    }
}
#[derive(Default)]
pub(super) struct Signatures {
    functions: BTreeMap<String, Positions>,
    methods: BTreeMap<String, Positions>,
    aliases: BTreeMap<String, BTreeMap<String, String>>,
    globs: BTreeMap<String, Vec<String>>,
    roots: BTreeSet<String>,
    // Indexes by final path segment, kept in step with `functions`/`methods`.
    function_names: BTreeMap<String, Vec<String>>,
    method_names: BTreeMap<String, Vec<String>>,
    // Pure-lookup memos (the maps are complete before any lookup; cleared on collect).
    path_memo: RefCell<HashMap<(String, String, String), BTreeSet<String>>>,
    function_memo: RefCell<HashMap<(String, String, String), Option<Positions>>>,
    method_memo: RefCell<HashMap<MethodKey, Option<Positions>>>,
}
type MethodKey = (String, String, Option<String>, String);
fn last_segment(path: &str) -> &str {
    path.rsplit("::").next().unwrap_or(path)
}
pub(super) fn type_path(ty: &Type) -> Option<String> {
    match ty {
        Type::Path(p) => Some(
            p.path
                .segments
                .iter()
                .map(|s| s.ident.to_string())
                .collect::<Vec<_>>()
                .join("::"),
        ),
        Type::Reference(r) => type_path(&r.elem),
        Type::Paren(p) => type_path(&p.elem),
        _ => None,
    }
}
impl Signatures {
    pub fn collect(&mut self, scope: Scope, tree: &syn::File) {
        self.roots.insert(scope.root.clone());
        self.path_memo.get_mut().clear();
        self.function_memo.get_mut().clear();
        self.method_memo.get_mut().clear();
        Collector {
            signatures: self,
            scope,
            owner: None,
        }
        .visit_file(tree);
    }
    pub fn paths(&self, scope: &Scope, raw: &str) -> BTreeSet<String> {
        let key = (scope.root.clone(), scope.path.clone(), raw.to_owned());
        if let Some(hit) = self.path_memo.borrow().get(&key) {
            return hit.clone();
        }
        let paths = self.expand(scope, raw, &mut BTreeSet::new());
        self.path_memo.borrow_mut().insert(key, paths.clone());
        paths
    }
    fn expand(
        &self,
        scope: &Scope,
        raw: &str,
        seen: &mut BTreeSet<(String, String)>,
    ) -> BTreeSet<String> {
        if !seen.insert((scope.path.clone(), raw.into())) {
            return BTreeSet::new();
        }
        let (first, rest) = raw.split_once("::").map_or((raw, ""), |(a, b)| (a, b));
        let suffix = if rest.is_empty() {
            String::new()
        } else {
            format!("::{rest}")
        };
        if first == "crate" {
            return BTreeSet::from([format!("{}{suffix}", scope.root)]);
        }
        if first == "self" {
            return BTreeSet::from([format!("{}{suffix}", scope.namespace())]);
        }
        if first == "super" {
            let parent = scope
                .namespace()
                .rsplit_once("::")
                .map_or(scope.root.as_str(), |(p, _)| p);
            return self.expand(
                &Scope {
                    root: scope.root.clone(),
                    path: parent.into(),
                },
                rest,
                seen,
            );
        }
        for parent in scope.parents() {
            if let Some(alias) = self.aliases.get(&parent).and_then(|a| a.get(first)) {
                return self.expand(
                    &Scope {
                        root: scope.root.clone(),
                        path: parent,
                    },
                    &format!("{alias}{suffix}"),
                    seen,
                );
            }
        }
        if self.roots.contains(first) || matches!(first, "std" | "tokio" | "thread") {
            return BTreeSet::from([raw.into()]);
        }
        let mut paths: BTreeSet<_> = scope
            .parents()
            .into_iter()
            .map(|p| format!("{p}::{raw}"))
            .collect();
        for parent in scope.parents() {
            if let Some(globs) = self.globs.get(&parent) {
                for glob in globs {
                    if !seen.insert((parent.clone(), format!("#glob:{glob}"))) {
                        continue;
                    }
                    paths.extend(self.expand(
                        &Scope {
                            root: scope.root.clone(),
                            path: parent.clone(),
                        },
                        &format!("{glob}::{raw}"),
                        seen,
                    ));
                }
            }
        }
        // Keep external spellings for Duration/spawn checks, without matching a
        // local signature by its final segment across a different crate.
        paths.insert(raw.into());
        paths
    }
    pub fn function(&self, scope: &Scope, raw: &str) -> Option<Positions> {
        let key = (scope.root.clone(), scope.path.clone(), raw.to_owned());
        if let Some(hit) = self.function_memo.borrow().get(&key) {
            return hit.clone();
        }
        let found = self.resolve_function(scope, raw);
        self.function_memo.borrow_mut().insert(key, found.clone());
        found
    }
    fn resolve_function(&self, scope: &Scope, raw: &str) -> Option<Positions> {
        // A definition in the current lexical module shadows enclosing/glob names.
        if !raw.contains("::") {
            for parent in scope.parents() {
                if self
                    .aliases
                    .get(&parent)
                    .is_some_and(|a| a.contains_key(raw))
                {
                    break;
                }
                if let Some(p) = self.functions.get(&format!("{parent}::{raw}")) {
                    return Some(p.clone());
                }
            }
        }
        let paths = self.paths(scope, raw);
        let mut found = None;
        for path in paths {
            if let Some(p) = self.functions.get(&path) {
                found.get_or_insert_with(Positions::new).extend(p);
            }
        }
        if found.is_none() && !raw.contains("::") {
            // Legacy syntax fixtures and unresolved local imports: conservative
            // within this crate only, and free functions only (never UFCS).
            let prefix = format!("{}::", scope.root);
            for path in self.function_names.get(raw).into_iter().flatten() {
                if path.starts_with(&prefix)
                    && !self.methods.contains_key(path)
                    && let Some(p) = self.functions.get(path)
                {
                    found.get_or_insert_with(Positions::new).extend(p);
                }
            }
        }
        found
    }
    pub fn method(&self, scope: &Scope, receiver: Option<&str>, name: &str) -> Option<Positions> {
        let key = (
            scope.root.clone(),
            scope.path.clone(),
            receiver.map(str::to_owned),
            name.to_owned(),
        );
        if let Some(hit) = self.method_memo.borrow().get(&key) {
            return hit.clone();
        }
        let found = self.resolve_method(scope, receiver, name);
        self.method_memo.borrow_mut().insert(key, found.clone());
        found
    }
    fn resolve_method(
        &self,
        scope: &Scope,
        receiver: Option<&str>,
        name: &str,
    ) -> Option<Positions> {
        let mut found = None;
        if let Some(receiver) = receiver {
            for path in self.paths(scope, receiver) {
                if let Some(p) = self.methods.get(&format!("{path}::{name}")) {
                    found.get_or_insert_with(Positions::new).extend(p);
                }
            }
            if found.is_some() {
                return found;
            }
        }
        // Unknown receiver types stay conservative in the caller's crate. No
        // whole-program inference or unrelated-crate name union is claimed.
        let prefix = format!("{}::", scope.root);
        for path in self.method_names.get(name).into_iter().flatten() {
            let Some(p) = self.methods.get(path) else {
                continue;
            };
            let owner = path.rsplit_once("::").map(|(owner, _)| owner).unwrap_or("");
            let owner_name = owner.rsplit("::").next().unwrap_or(owner);
            if path.starts_with(&prefix) || self.paths(scope, owner_name).contains(owner) {
                found.get_or_insert_with(Positions::new).extend(p);
            }
        }
        found
    }
}
struct Collector<'a> {
    signatures: &'a mut Signatures,
    scope: Scope,
    owner: Option<String>,
}
impl Collector<'_> {
    fn import(&mut self, prefix: &str, tree: &syn::UseTree) {
        match tree {
            syn::UseTree::Path(p) => self.import(&format!("{prefix}{}::", p.ident), &p.tree),
            syn::UseTree::Group(g) => {
                for item in &g.items {
                    self.import(prefix, item);
                }
            }
            syn::UseTree::Glob(_) => self
                .signatures
                .globs
                .entry(self.scope.path.clone())
                .or_default()
                .push(prefix.trim_end_matches("::").into()),
            syn::UseTree::Name(n) => {
                self.signatures
                    .aliases
                    .entry(self.scope.path.clone())
                    .or_default()
                    .insert(n.ident.to_string(), format!("{prefix}{}", n.ident));
            }
            syn::UseTree::Rename(r) => {
                self.signatures
                    .aliases
                    .entry(self.scope.path.clone())
                    .or_default()
                    .insert(r.rename.to_string(), format!("{prefix}{}", r.ident));
            }
        }
    }
    fn signature(&mut self, sig: &syn::Signature) {
        let method = sig.receiver().is_some();
        let positions: Positions = sig
            .inputs
            .iter()
            .filter(|a| !matches!(a, FnArg::Receiver(_)))
            .enumerate()
            .filter_map(|(i, a)| {
                if let FnArg::Typed(a) = a
                    && let Pat::Ident(p) = &*a.pat
                    && super::time_name(&p.ident.to_string())
                {
                    Some(i)
                } else {
                    None
                }
            })
            .collect();
        let owner = self.owner.as_deref().unwrap_or(&self.scope.path);
        let path = format!("{owner}::{}", sig.ident);
        let signatures = &mut *self.signatures;
        if !signatures.functions.contains_key(&path) {
            signatures
                .function_names
                .entry(last_segment(&path).to_owned())
                .or_default()
                .push(path.clone());
        }
        if method {
            if !signatures.methods.contains_key(&path) {
                signatures
                    .method_names
                    .entry(last_segment(&path).to_owned())
                    .or_default()
                    .push(path.clone());
            }
            signatures
                .methods
                .entry(path.clone())
                .or_default()
                .extend(&positions);
            signatures
                .functions
                .entry(path)
                .or_default()
                .extend(positions.iter().map(|i| i + 1));
        } else {
            signatures
                .functions
                .entry(path)
                .or_default()
                .extend(positions);
        }
    }
}
impl<'ast> Visit<'ast> for Collector<'_> {
    fn visit_block(&mut self, block: &'ast syn::Block) {
        let old = self.scope.clone();
        self.scope = old.block(block);
        visit::visit_block(self, block);
        self.scope = old;
    }
    fn visit_item_mod(&mut self, item: &'ast syn::ItemMod) {
        let old = self.scope.clone();
        self.scope = old.child(&item.ident.to_string());
        visit::visit_item_mod(self, item);
        self.scope = old;
    }
    fn visit_item_use(&mut self, item: &'ast syn::ItemUse) {
        self.import("", &item.tree);
    }
    fn visit_item_type(&mut self, item: &'ast syn::ItemType) {
        if let Some(path) = type_path(&item.ty) {
            self.signatures
                .aliases
                .entry(self.scope.path.clone())
                .or_default()
                .insert(item.ident.to_string(), path);
        }
    }
    fn visit_item_impl(&mut self, item: &'ast syn::ItemImpl) {
        let old = self.owner.clone();
        self.owner = type_path(&item.self_ty).map(|ty| format!("{}::{ty}", self.scope.path));
        visit::visit_item_impl(self, item);
        self.owner = old;
    }
    fn visit_item_trait(&mut self, item: &'ast syn::ItemTrait) {
        let old = self.owner.clone();
        self.owner = Some(format!("{}::{}", self.scope.path, item.ident));
        visit::visit_item_trait(self, item);
        self.owner = old;
    }
    fn visit_signature(&mut self, sig: &'ast syn::Signature) {
        self.signature(sig);
    }
}
