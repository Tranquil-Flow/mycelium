// SPDX-License-Identifier: AGPL-3.0-only
// Conditions that are decided by a literal, in every shape the policy names.
pub fn drain(items: &mut Vec<u8>) -> usize {
    while false && items.len() > 1 { // expect: CG001
        items.pop();
    }
    if true || items.is_empty() { // expect: CG001
        items.clear();
    }
    if false { // expect: CG001
        items.push(1);
    }
    if !true { // expect: CG001
        items.push(2);
    }
    while true { // expect: CG001
        break;
    }
    if items.is_empty() && true { // expect: CG001
        items.push(3);
    }
    if false || items.len() > 4 { // expect: CG001
        items.push(4);
    }
    items.len()
}
