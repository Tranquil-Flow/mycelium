// SPDX-License-Identifier: AGPL-3.0-only
//! Exact bounded rational arithmetic for the legacy deterministic splitter.
use crate::{Error, Result};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Ratio {
    pub n: u128,
    pub d: u128,
}
fn gcd(mut a: u128, mut b: u128) -> u128 {
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}
impl Ratio {
    pub fn new(n: u128, d: u128) -> Result<Self> {
        if d == 0 {
            return Err(Error::new("invalid_policy"));
        }
        let g = gcd(n, d);
        Ok(Self { n: n / g, d: d / g })
    }
    pub fn int(n: u64) -> Self {
        Self { n: n.into(), d: 1 }
    }
    pub fn float(v: f64) -> Result<Self> {
        if !v.is_finite() || v <= 0.0 {
            return Err(Error::new("member_capability_invalid"));
        }
        let bits = v.to_bits();
        let exp = ((bits >> 52) & 0x7ff) as i32;
        let mut n = (bits & ((1u64 << 52) - 1)) as u128;
        let mut e = if exp == 0 {
            -1074
        } else {
            n |= 1u128 << 52;
            exp - 1023 - 52
        };
        while n.is_multiple_of(2) {
            n /= 2;
            e += 1;
        }
        if e >= 0 {
            let shift = u32::try_from(e).map_err(|_| Error::overflow())?;
            if shift >= 128 || n > u128::MAX >> shift {
                return Err(Error::overflow());
            }
            Self::new(n << shift, 1)
        } else {
            let shift = (-e) as u32;
            Self::new(n, 1u128.checked_shl(shift).ok_or_else(Error::overflow)?)
        }
    }
    pub fn add(self, b: Self) -> Result<Self> {
        let g = gcd(self.d, b.d);
        Self::new(
            self.n
                .checked_mul(b.d / g)
                .and_then(|x| b.n.checked_mul(self.d / g).and_then(|y| x.checked_add(y)))
                .ok_or_else(Error::overflow)?,
            self.d.checked_mul(b.d / g).ok_or_else(Error::overflow)?,
        )
    }
    pub fn mul(self, b: Self) -> Result<Self> {
        let g = gcd(self.n, b.d);
        let h = gcd(b.n, self.d);
        Self::new(
            (self.n / g)
                .checked_mul(b.n / h)
                .ok_or_else(Error::overflow)?,
            (self.d / h)
                .checked_mul(b.d / g)
                .ok_or_else(Error::overflow)?,
        )
    }
    pub fn div(self, b: Self) -> Result<Self> {
        self.mul(Self::new(b.d, b.n)?)
    }
    pub fn cmp(self, b: Self) -> Result<std::cmp::Ordering> {
        let g = gcd(self.d, b.d);
        Ok(self
            .n
            .checked_mul(b.d / g)
            .ok_or_else(Error::overflow)?
            .cmp(&b.n.checked_mul(self.d / g).ok_or_else(Error::overflow)?))
    }
    pub fn distance(self, b: Self) -> Result<Self> {
        let g = gcd(self.d, b.d);
        let a = self.n.checked_mul(b.d / g).ok_or_else(Error::overflow)?;
        let z = b.n.checked_mul(self.d / g).ok_or_else(Error::overflow)?;
        Self::new(
            a.abs_diff(z),
            self.d.checked_mul(b.d / g).ok_or_else(Error::overflow)?,
        )
    }
    pub fn floor(self) -> Result<u64> {
        u64::try_from(self.n / self.d).map_err(|_| Error::overflow())
    }
    pub fn ceil(self) -> Result<u64> {
        u64::try_from(self.n / self.d + u128::from(!self.n.is_multiple_of(self.d)))
            .map_err(|_| Error::overflow())
    }
}
