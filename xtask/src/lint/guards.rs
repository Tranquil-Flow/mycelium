// SPDX-License-Identifier: AGPL-3.0-only
//! CG001: no constant guards in test code. A condition decided by a literal (`if false`,
//! `while false && ..`, `if true || ..`, `assert!(true)`, a match guard `if true`, a bool
//! `const` used as the condition, or a literal operand of `&&`/`||`) makes a test or its
//! support vacuous: the guarded code never runs, or the check never fails.
//!
//! Test code is: files under a `tests`, `test-support` or `test_support` directory (or named
//! `tests.rs`), and, in any file, items under `#[cfg(test)]` or `#[test]`.
use super::Diagnostic;
use std::collections::BTreeMap;
use syn::{
    Expr, Item,
    parse::Parser,
    spanned::Spanned,
    visit::{self, Visit},
};

fn path_is_test(path: &str) -> bool {
    let mut parts: Vec<&str> = path.split('/').collect();
    let file = parts.pop().unwrap_or_default();
    parts
        .iter()
        .any(|p| matches!(*p, "tests" | "test-support" | "test_support"))
        || matches!(file, "tests.rs" | "test_support.rs")
}
fn test_attribute(attribute: &syn::Attribute) -> bool {
    let path = attribute.path();
    if path.segments.last().is_some_and(|s| s.ident == "test") {
        return true;
    }
    if !path.is_ident("cfg") {
        return false;
    }
    fn mentions_test(tokens: proc_macro2::TokenStream) -> bool {
        tokens.into_iter().any(|token| match token {
            proc_macro2::TokenTree::Ident(i) => i == "test",
            proc_macro2::TokenTree::Group(g) => mentions_test(g.stream()),
            _ => false,
        })
    }
    attribute
        .meta
        .require_list()
        .is_ok_and(|list| mentions_test(list.tokens.clone()))
}
fn peel(expr: &Expr) -> &Expr {
    match expr {
        Expr::Paren(p) => peel(&p.expr),
        Expr::Group(g) => peel(&g.expr),
        _ => expr,
    }
}

struct Guards<'a> {
    path: &'a str,
    test_depth: usize,
    /// `const NAME: bool = <literal>`; `None` when declarations of the name disagree.
    consts: BTreeMap<String, Option<bool>>,
    findings: &'a mut Vec<Diagnostic>,
}
impl Guards<'_> {
    fn active(&self) -> bool {
        self.test_depth > 0 || path_is_test(self.path)
    }
    fn literal(&self, expr: &Expr) -> Option<bool> {
        match peel(expr) {
            Expr::Lit(lit) => match &lit.lit {
                syn::Lit::Bool(value) => Some(value.value),
                _ => None,
            },
            Expr::Unary(unary) if matches!(unary.op, syn::UnOp::Not(_)) => {
                self.literal(&unary.expr).map(|v| !v)
            }
            Expr::Path(path) if path.path.segments.len() == 1 => self
                .consts
                .get(&path.path.segments[0].ident.to_string())
                .copied()
                .flatten(),
            Expr::Binary(binary) => {
                let (left, right) = (self.literal(&binary.left), self.literal(&binary.right));
                match binary.op {
                    syn::BinOp::And(_) if left == Some(false) || right == Some(false) => {
                        Some(false)
                    }
                    syn::BinOp::And(_) if left == Some(true) && right == Some(true) => Some(true),
                    syn::BinOp::Or(_) if left == Some(true) || right == Some(true) => Some(true),
                    syn::BinOp::Or(_) if left == Some(false) && right == Some(false) => Some(false),
                    _ => None,
                }
            }
            _ => None,
        }
    }
    /// Decided by a literal, or has a literal operand somewhere in its `&&`/`||`/`!` tree.
    fn constant(&self, expr: &Expr) -> bool {
        if self.literal(expr).is_some() {
            return true;
        }
        match peel(expr) {
            Expr::Binary(binary) if matches!(binary.op, syn::BinOp::And(_) | syn::BinOp::Or(_)) => {
                self.constant(&binary.left) || self.constant(&binary.right)
            }
            Expr::Unary(unary) if matches!(unary.op, syn::UnOp::Not(_)) => {
                self.constant(&unary.expr)
            }
            _ => false,
        }
    }
    fn check(&mut self, condition: &Expr) {
        if self.active() && self.constant(condition) {
            self.findings.push(Diagnostic {
                path: self.path.to_string(),
                line: condition.span().start().line,
                code: "CG001".to_string(),
            });
        }
    }
    fn with_attributes(&mut self, attributes: &[syn::Attribute], walk: impl FnOnce(&mut Self)) {
        let tested = attributes.iter().any(test_attribute);
        self.test_depth += usize::from(tested);
        walk(self);
        self.test_depth -= usize::from(tested);
    }
    fn macro_tokens(&mut self, name: &str, tokens: proc_macro2::TokenStream) {
        if let Ok(expressions) =
            syn::punctuated::Punctuated::<Expr, syn::Token![,]>::parse_terminated
                .parse2(tokens.clone())
        {
            if matches!(name, "assert" | "debug_assert")
                && let Some(first) = expressions.first()
            {
                self.check(first);
            }
            for expression in expressions {
                self.visit_expr(&expression);
            }
        } else if let Ok(statements) = syn::Block::parse_within.parse2(tokens) {
            for statement in statements {
                self.visit_stmt(&statement);
            }
        }
    }
}
impl<'ast> Visit<'ast> for Guards<'_> {
    fn visit_item_mod(&mut self, item: &'ast syn::ItemMod) {
        self.with_attributes(&item.attrs, |this| visit::visit_item_mod(this, item));
    }
    fn visit_item_fn(&mut self, item: &'ast syn::ItemFn) {
        self.with_attributes(&item.attrs, |this| visit::visit_item_fn(this, item));
    }
    fn visit_item_impl(&mut self, item: &'ast syn::ItemImpl) {
        self.with_attributes(&item.attrs, |this| visit::visit_item_impl(this, item));
    }
    fn visit_impl_item_fn(&mut self, item: &'ast syn::ImplItemFn) {
        self.with_attributes(&item.attrs, |this| visit::visit_impl_item_fn(this, item));
    }
    fn visit_expr_if(&mut self, branch: &'ast syn::ExprIf) {
        self.check(&branch.cond);
        visit::visit_expr_if(self, branch);
    }
    fn visit_expr_while(&mut self, looped: &'ast syn::ExprWhile) {
        self.check(&looped.cond);
        visit::visit_expr_while(self, looped);
    }
    fn visit_arm(&mut self, arm: &'ast syn::Arm) {
        if let Some((_, guard)) = &arm.guard {
            self.check(guard);
        }
        visit::visit_arm(self, arm);
    }
    fn visit_macro(&mut self, mac: &'ast syn::Macro) {
        let name = mac
            .path
            .segments
            .last()
            .map(|s| s.ident.to_string())
            .unwrap_or_default();
        self.macro_tokens(&name, mac.tokens.clone());
    }
}

fn collect_consts(items: &[Item], consts: &mut BTreeMap<String, Option<bool>>) {
    for item in items {
        match item {
            Item::Const(constant) => {
                let is_bool =
                    matches!(&*constant.ty, syn::Type::Path(p) if p.path.is_ident("bool"));
                if let (true, Expr::Lit(lit)) = (is_bool, &*constant.expr)
                    && let syn::Lit::Bool(value) = &lit.lit
                {
                    let name = constant.ident.to_string();
                    let merged = match consts.get(&name) {
                        Some(Some(old)) if *old != value.value => None,
                        Some(None) => None,
                        _ => Some(value.value),
                    };
                    consts.insert(name, merged);
                }
            }
            Item::Mod(module) => {
                if let Some((_, inner)) = &module.content {
                    collect_consts(inner, consts);
                }
            }
            _ => {}
        }
    }
}

pub fn scan(path: &str, file: &syn::File, findings: &mut Vec<Diagnostic>) {
    let mut consts = BTreeMap::new();
    collect_consts(&file.items, &mut consts);
    let mut guards = Guards {
        path,
        test_depth: 0,
        consts,
        findings,
    };
    guards.visit_file(file);
}
