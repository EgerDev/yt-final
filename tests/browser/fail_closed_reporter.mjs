export default class FailClosedReporter {
  constructor() {
    this.discovered = 0;
    this.expectedFailures = [];
    this.skipped = [];
  }

  onBegin(_config, suite) {
    const tests = suite.allTests();
    this.discovered = tests.length;
    this.expectedFailures = tests
      .filter((test) => test.expectedStatus !== "passed")
      .map((test) => test.titlePath().join(" > "));
  }

  onTestEnd(test, result) {
    if (result.status === "skipped") {
      this.skipped.push(test.titlePath().join(" > "));
    }
  }

  onEnd(result) {
    if (
      this.discovered === 0
      || this.expectedFailures.length > 0
      || this.skipped.length > 0
    ) {
      let detail = "no browser tests were discovered";
      if (this.expectedFailures.length > 0) {
        detail = (
          `expected-status browser tests: ${this.expectedFailures.join(", ")}`
        );
      } else if (this.skipped.length > 0) {
        detail = `skipped browser tests: ${this.skipped.join(", ")}`;
      }
      process.stderr.write(`Fail-closed browser gate: ${detail}\n`);
      return { status: "failed" };
    }
    return { status: result.status };
  }
}
