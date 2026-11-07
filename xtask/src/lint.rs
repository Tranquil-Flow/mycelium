// SPDX-License-Identifier: AGPL-3.0-only
//! Conservative source policy, not a type checker or a macro expander.
use std::collections::{BTreeMap, BTreeSet};
use syn::{
    Expr, FnArg, Pat, Stmt,
    parse::Parser,
    spanned::Spanned,
    visit::{self, Visit},
};

pub const RULES: &[&str] = &[
    "SE001", "SE002", "SE003", "SE004", "SE005", "TM001", "TM002", "CR001", "CG001",
];

#[derive(Debug, Clone)]
pub struct Source {
    pub path: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Diagnostic {
    pub path: String,
    pub line: usize,
    pub code: String,
}

pub fn explanation(code: &str) -> &'static str {
    match code {
        "SE001" => "wildcard discards a call; propagate or record the failure",
        "SE002" => "discarded .ok() hides the error",
        "SE003" => "empty Err handler; propagate or record a specific failure",
        "SE004" => "spawned JoinHandle discarded; retain and observe completion",
        "SE005" => "cleanup error can fall through to an unconditional success",
        "TM001" => "literal Duration argument outside crates/budgets",
        "TM002" => "literal timeout/deadline/ttl argument outside crates/budgets",
        "CR001" => "hand-written cryptography (RU-010): use the admitted audited crates",
        "CG001" => "constant guard in test code: the guarded code or the check is vacuous",
        _ => "unknown policy rule",
    }
}

mod crypto;
mod guards;
mod resolver;
use resolver::{Scope, Signatures, type_path};

pub fn scan_sources(sources: &[Source]) -> Result<Vec<Diagnostic>, String> {
    let mut parsed = Vec::new();
    let mut signatures = Signatures::default();
    for source in sources {
        let tree = syn::parse_file(&source.text).map_err(|error| {
            format!(
                "{}:{}: PARSE: {}",
                source.path,
                error.span().start().line,
                error
            )
        })?;
        signatures.collect(Scope::source(&source.path), &tree);
        parsed.push(tree);
    }
    let mut findings = Vec::new();
    for (source, tree) in sources.iter().zip(&parsed) {
        let mut visitor = Policy {
            path: &source.path,
            budget: source.path.starts_with("crates/budgets/"),
            signatures: &signatures,
            scope: Scope::source(&source.path),
            receiver_types: BTreeMap::new(),
            owner: None,
            findings: &mut findings,
        };
        visitor.visit_file(tree);
        crypto::scan(&source.path, &source.text, &mut findings);
        guards::scan(&source.path, tree, &mut findings);
    }
    findings.sort();
    findings.dedup();
    Ok(findings)
}

fn time_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    ["timeout", "deadline", "ttl"]
        .iter()
        .any(|word| lower.contains(word))
}

fn peel(expr: &Expr) -> &Expr {
    match expr {
        Expr::Paren(p) => peel(&p.expr),
        Expr::Group(g) => peel(&g.expr),
        _ => expr,
    }
}

fn number(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::Lit(lit) => matches!(lit.lit, syn::Lit::Int(_) | syn::Lit::Float(_)),
        Expr::Unary(unary) if matches!(unary.op, syn::UnOp::Neg(_)) => number(&unary.expr),
        Expr::Cast(cast) => number(&cast.expr),
        _ => false,
    }
}

fn wildcard(pat: &Pat) -> bool {
    match pat {
        Pat::Wild(_) => true,
        Pat::Type(typed) => wildcard(&typed.pat),
        Pat::Paren(paren) => wildcard(&paren.pat),
        _ => false,
    }
}

fn call_discarded(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::Call(_) | Expr::MethodCall(_) => true,
        Expr::Await(awaited) => call_discarded(&awaited.base),
        // `let _ = operation()?` has already propagated the failure.
        _ => false,
    }
}

fn err_pattern(pat: &Pat) -> bool {
    match pat {
        Pat::TupleStruct(tuple) => tuple.path.segments.last().is_some_and(|s| s.ident == "Err"),
        Pat::Or(or) => or.cases.iter().any(err_pattern),
        Pat::Paren(paren) => err_pattern(&paren.pat),
        _ => false,
    }
}

fn err_condition(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::Let(condition) => err_pattern(&condition.pat),
        Expr::Binary(binary) if matches!(binary.op, syn::BinOp::And(_)) => {
            err_condition(&binary.left) || err_condition(&binary.right)
        }
        _ => false,
    }
}

fn empty_expr(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::Block(block) => empty_block(&block.block),
        Expr::Tuple(tuple) => tuple.elems.is_empty(),
        _ => false,
    }
}
fn empty_block(block: &syn::Block) -> bool {
    block
        .stmts
        .iter()
        .all(|stmt| matches!(stmt, Stmt::Expr(expr, _) if empty_expr(expr)))
}

fn path_name(expr: &Expr) -> Option<String> {
    match peel(expr) {
        Expr::Path(path) => Some(
            path.path
                .segments
                .iter()
                .map(|s| s.ident.to_string())
                .collect::<Vec<_>>()
                .join("::"),
        ),
        _ => None,
    }
}
fn last_name(expr: &Expr) -> Option<String> {
    match peel(expr) {
        Expr::Call(call) => {
            path_name(&call.func).map(|p| p.rsplit("::").next().unwrap_or(&p).to_string())
        }
        Expr::MethodCall(call) => Some(call.method.to_string()),
        Expr::Await(awaited) => last_name(&awaited.base),
        _ => None,
    }
}
fn cleanup_call(expr: &Expr) -> bool {
    last_name(expr).is_some_and(|name| {
        name.split('_')
            .any(|word| matches!(word, "stop" | "close" | "cleanup" | "teardown" | "join"))
    })
}

// This deliberately recognizes only explicit, unconditional exits. A return in
// a nested conditional is not proof that every error path exits.
fn exits(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::Return(_) | Expr::Break(_) | Expr::Continue(_) => true,
        Expr::Block(block) => block_exits(&block.block),
        Expr::If(branch) => {
            block_exits(&branch.then_branch)
                && branch
                    .else_branch
                    .as_ref()
                    .is_some_and(|(_, other)| exits(other))
        }
        Expr::Match(matched) => {
            !matched.arms.is_empty() && matched.arms.iter().all(|arm| exits(&arm.body))
        }
        _ => false,
    }
}
fn block_exits(block: &syn::Block) -> bool {
    block
        .stmts
        .iter()
        .any(|stmt| matches!(stmt, Stmt::Expr(expr, _) if exits(expr)))
}
fn success(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::Return(ret) => ret.expr.as_ref().is_some_and(|expr| success(expr)),
        Expr::Block(block) => block
            .block
            .stmts
            .last()
            .is_some_and(|s| matches!(s, Stmt::Expr(e, _) if success(e))),
        other => last_name(other).is_some_and(|name| {
            matches!(
                name.as_str(),
                "Ok" | "record_success" | "mark_stopped" | "mark_cleanup_complete"
            )
        }),
    }
}
fn failed_cleanup_falls_through(expr: &Expr) -> bool {
    match peel(expr) {
        Expr::If(branch) => match peel(&branch.cond) {
            Expr::Let(condition)
                if err_pattern(&condition.pat) && cleanup_call(&condition.expr) =>
            {
                !block_exits(&branch.then_branch)
            }
            _ => false,
        },
        Expr::Match(matched) if cleanup_call(&matched.expr) => matched
            .arms
            .iter()
            .any(|arm| err_pattern(&arm.pat) && !exits(&arm.body)),
        _ => false,
    }
}

struct Policy<'a> {
    path: &'a str,
    budget: bool,
    signatures: &'a Signatures,
    scope: Scope,
    receiver_types: BTreeMap<String, String>,
    owner: Option<String>,
    findings: &'a mut Vec<Diagnostic>,
}
impl Policy<'_> {
    fn emit(&mut self, span: proc_macro2::Span, code: &str) {
        self.findings.push(Diagnostic {
            path: self.path.into(),
            line: span.start().line,
            code: code.into(),
        });
    }
    fn parameters(&mut self, sig: &syn::Signature) {
        for arg in &sig.inputs {
            if let FnArg::Typed(arg) = arg
                && let Pat::Ident(name) = &*arg.pat
                && let Some(ty) = type_path(&arg.ty)
            {
                self.receiver_types.insert(name.ident.to_string(), ty);
            }
        }
        if let Some(owner) = &self.owner {
            self.receiver_types.insert("self".into(), owner.clone());
        }
    }
    fn receiver(&self, expr: &Expr) -> Option<String> {
        match peel(expr) {
            Expr::Path(p) if p.path.segments.len() == 1 => self
                .receiver_types
                .get(&p.path.segments[0].ident.to_string())
                .cloned(),
            Expr::Reference(r) => self.receiver(&r.expr),
            _ => None,
        }
    }
    fn macro_tokens(&mut self, tokens: proc_macro2::TokenStream) {
        // Standard expression-list / statement macros retain original spans.
        // This is syntax inspection, not execution of proc macros or DSLs.
        if let Ok(expressions) =
            syn::punctuated::Punctuated::<Expr, syn::Token![,]>::parse_terminated
                .parse2(tokens.clone())
        {
            for expression in expressions {
                self.visit_expr(&expression);
            }
        } else if let Ok(statements) = syn::Block::parse_within.parse2(tokens.clone()) {
            for statement in statements {
                self.visit_stmt(&statement);
            }
        } else {
            for token in tokens {
                if let proc_macro2::TokenTree::Group(group) = token {
                    self.macro_tokens(group.stream());
                }
            }
        }
    }
    fn is_spawn(&self, expr: &Expr) -> bool {
        path_name(expr).is_some_and(|name| {
            self.signatures
                .paths(&self.scope, &name)
                .iter()
                .any(|path| {
                    matches!(
                        path.as_str(),
                        "thread::spawn"
                            | "std::thread::spawn"
                            | "tokio::spawn"
                            | "tokio::task::spawn"
                            | "tokio::task::spawn_blocking"
                    )
                })
        })
    }
    fn timed_args<'a>(
        &mut self,
        name: &str,
        known: Option<BTreeSet<usize>>,
        args: impl Iterator<Item = &'a Expr>,
    ) {
        if self.budget {
            return;
        }
        for (index, arg) in args.enumerate() {
            let timed = match &known {
                Some(positions) => positions.contains(&index),
                None => time_name(name.rsplit("::").next().unwrap_or(name)), // External API fallback.
            };
            if timed && number(arg) {
                self.emit(arg.span(), "TM002");
            }
        }
    }
}
impl<'ast> Visit<'ast> for Policy<'_> {
    fn visit_item_mod(&mut self, item: &'ast syn::ItemMod) {
        let old = self.scope.clone();
        self.scope = old.child(&item.ident.to_string());
        visit::visit_item_mod(self, item);
        self.scope = old;
    }
    fn visit_item_impl(&mut self, item: &'ast syn::ItemImpl) {
        let old = self.owner.clone();
        self.owner = type_path(&item.self_ty);
        visit::visit_item_impl(self, item);
        self.owner = old;
    }
    fn visit_item_fn(&mut self, item: &'ast syn::ItemFn) {
        let old = std::mem::take(&mut self.receiver_types);
        self.parameters(&item.sig);
        visit::visit_item_fn(self, item);
        self.receiver_types = old;
    }
    fn visit_impl_item_fn(&mut self, item: &'ast syn::ImplItemFn) {
        let old = std::mem::take(&mut self.receiver_types);
        self.parameters(&item.sig);
        visit::visit_impl_item_fn(self, item);
        self.receiver_types = old;
    }

    fn visit_macro(&mut self, mac: &'ast syn::Macro) {
        self.macro_tokens(mac.tokens.clone());
    }
    fn visit_local(&mut self, local: &'ast syn::Local) {
        if wildcard(&local.pat)
            && local
                .init
                .as_ref()
                .is_some_and(|init| call_discarded(&init.expr))
        {
            self.emit(local.span(), "SE001");
        }
        visit::visit_local(self, local);
        if let Pat::Type(pat) = &local.pat
            && let Pat::Ident(name) = &*pat.pat
            && let Some(ty) = type_path(&pat.ty)
        {
            self.receiver_types.insert(name.ident.to_string(), ty);
        } else if let Pat::Ident(name) = &local.pat {
            // Unannotated shadowing must not retain an old receiver's type.
            self.receiver_types.remove(&name.ident.to_string());
        }
    }
    fn visit_stmt(&mut self, stmt: &'ast Stmt) {
        if let Stmt::Expr(expr, Some(_)) = stmt {
            match peel(expr) {
                Expr::MethodCall(call) if call.method == "ok" && call.args.is_empty() => {
                    self.emit(expr.span(), "SE002")
                }
                Expr::Call(call) if self.is_spawn(&call.func) => {
                    self.emit(expr.span(), "SE004");
                }
                _ => {}
            }
        }
        visit::visit_stmt(self, stmt);
    }
    fn visit_arm(&mut self, arm: &'ast syn::Arm) {
        if err_pattern(&arm.pat) && empty_expr(&arm.body) {
            self.emit(arm.pat.span(), "SE003");
        }
        visit::visit_arm(self, arm);
    }
    fn visit_expr_match(&mut self, matched: &'ast syn::ExprMatch) {
        if cleanup_call(&matched.expr) {
            for arm in &matched.arms {
                if err_pattern(&arm.pat) && success(&arm.body) {
                    self.emit(arm.body.span(), "SE005");
                }
            }
        }
        visit::visit_expr_match(self, matched);
    }
    fn visit_expr_if(&mut self, branch: &'ast syn::ExprIf) {
        if let Expr::Let(condition) = peel(&branch.cond)
            && err_pattern(&condition.pat)
            && cleanup_call(&condition.expr)
        {
            for statement in &branch.then_branch.stmts {
                if let Stmt::Expr(expr, _) = statement
                    && success(expr)
                {
                    self.emit(expr.span(), "SE005");
                }
            }
        }
        if err_condition(&branch.cond) && empty_block(&branch.then_branch) {
            self.emit(branch.span(), "SE003");
        }
        visit::visit_expr_if(self, branch);
    }
    fn visit_block(&mut self, block: &'ast syn::Block) {
        let mut unproven = false;
        for stmt in &block.stmts {
            if let Stmt::Expr(expr, _) = stmt {
                if unproven && success(expr) {
                    self.emit(expr.span(), "SE005");
                }
                if failed_cleanup_falls_through(expr) {
                    unproven = true;
                }
                if exits(expr) {
                    unproven = false;
                }
            }
        }
        let scope = self.scope.clone();
        let receivers = self.receiver_types.clone();
        self.scope = scope.block(block);
        visit::visit_block(self, block);
        self.scope = scope;
        self.receiver_types = receivers;
    }
    fn visit_expr_call(&mut self, call: &'ast syn::ExprCall) {
        if let Some(raw) = path_name(&call.func) {
            for path in self.signatures.paths(&self.scope, &raw) {
                let names = path.split("::").collect::<Vec<_>>();
                let name = names.last().copied().unwrap_or_default();
                if !self.budget
                    && names.len() >= 2
                    && names[names.len() - 2] == "Duration"
                    && matches!(
                        name,
                        "from_secs"
                            | "from_millis"
                            | "from_secs_f64"
                            | "from_secs_f32"
                            | "from_micros"
                            | "from_nanos"
                    )
                {
                    for arg in &call.args {
                        if number(arg) {
                            self.emit(arg.span(), "TM001");
                        }
                    }
                }
            }
            self.timed_args(
                &raw,
                self.signatures.function(&self.scope, &raw),
                call.args.iter(),
            );
        }
        visit::visit_expr_call(self, call);
    }
    fn visit_expr_method_call(&mut self, call: &'ast syn::ExprMethodCall) {
        let name = call.method.to_string();
        let receiver = self.receiver(&call.receiver);
        let known = self
            .signatures
            .method(&self.scope, receiver.as_deref(), &name);
        self.timed_args(&name, known, call.args.iter());
        visit::visit_expr_method_call(self, call);
    }
}
