# Who must handle an RFQ after extraction (S4). Export-controlled parts (ITAR, EAR...) always go to a senior
# estimator; everything else to an estimator. The caller treats "no answer" as "senior" (fail closed).
package apex.extraction

import rego.v1

default route := "estimator"

route := "senior" if {
	input.export_control != null
	input.export_control != ""
}
