pub fn increment(value: u32) -> u32 {
    value + 1
}

#[cfg(test)]
mod tests {
    use super::increment;

    #[test]
    fn increments_normal_values() {
        assert_eq!(increment(41), 42);
    }
}
